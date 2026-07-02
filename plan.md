# 感冒退烧 OTC AI 导购系统 Plan

## 架构概览

系统分为 **7 个模块**，围绕 LangGraph 状态图编排：

```
┌─────────────────────────────────────────────────────────┐
│                      FastAPI                            │
│  POST /api/v1/chat          (SSE 流式)                   │
│  POST /api/v1/sessions       (创建会话)                  │
│  GET  /health                (健康检查)                  │
└──────────────────────┬──────────────────────────────────┘
                       │ 每次用户消息触发一次 Graph Run
                       ▼
┌─────────────────────────────────────────────────────────┐
│                   LangGraph                             │
│                                                         │
│  ┌──────────┐     ┌──────────────────────┐             │
│  │  Intake  │────▶│     Dispatcher        │             │
│  │ 消息预处理│     │     对话调度器          │             │
│  └──────────┘     │ LLM: 理解上下文+意图   │             │
│                   │ 决定路由目标 + 提取参数 │             │
│                   └───────────┬───────────┘             │
│                               │ 条件边                    │
│              ┌────────────────┼────────────────┐        │
│              ▼                ▼                ▼        │
│     ┌──────────┐      ┌──────────┐      ┌──────────┐   │
│     │ Consult  │      │ Explain  │      │   End    │   │
│     │(ReAct追问)│      │(RAG解释) │      │(结束会话) │   │
│     └────┬─────┘      └──────────┘      └──────────┘   │
│          │                                               │
│     ┌────▼─────┐  ← slots 充分后同轮继续                  │
│     │  Safety  │                                         │
│     │  Check   │                                         │
│     └────┬─────┘                                         │
│          │ PASS                                          │
│     ┌────▼─────┐                                         │
│     │ Recommend│◄── 换药/重排时 Dispatcher 路由到此处      │
│     └────┬─────┘                                         │
│          │                                               │
│     ┌────▼─────┐                                         │
│     │Inventory │                                         │
│     └──────────┘                                         │
└─────────────────────────────────────────────────────────┘
                       │
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
   ┌─────────┐  ┌──────────┐  ┌──────────────────┐
   │PostgreSQL│  │  Milvus  │  │ LLM (OpenAI 兼容) │
   │药品/库存 │  │说明书RAG │  │ Qwen / 任意模型    │
   │会话/消息 │  │向量检索   │  │ DashScope / 任意源 │
   └─────────┘  └──────────┘  └──────────────────┘
```

**核心设计决策**：每个用户消息触发一次完整的 Graph Run。Consult 节点的 ReAct 循环**跨多个 Graph Run**（每轮追问一条消息，下轮用户回复后继续），而不是在一个 Run 内阻塞循环。这样天然支持话题切换——每次 Run 都从 Intake 和 Dispatcher 开始。

## 核心数据结构

### ConversationState（Graph 状态）

整个 Graph 共享的状态对象，跨节点、跨轮次持久化：

```python
class ConversationState(TypedDict):
    session_id: str
    messages: list[Message]           # {role, content, timestamp}
    phase: str                        # "consulting" | "recommending" | "explaining" | "ended"
    previous_phase: str | None        # 跳转前的阶段，用于回归
    consult_slots: dict               # 结构化症状信息
    dispatcher_result: dict           # 本轮 Dispatcher 决策 {route, intent, params}
    safety_result: dict | None        # 安全检查结果 {verdict, triggered_rules, message}
    recommendations: list[dict]       # 推荐结果 {drug_id, generic_name, brand_name, match_reason, score}
    response: str                     # 本轮待返回给用户的内容
```

### ConsultSlots（问诊槽位）

```python
class ConsultSlots(TypedDict):
    symptoms: list[dict]              # [{name, location, severity, onset}]
    temperature: float | None
    duration_days: int | None
    medications_taken: list[str]
    special_population: str | None    # "pregnant" | "breastfeeding" | "child" | "elderly"
    age: int | None
    chronic_conditions: list[str]
    allergies: list[str]
    other_symptoms: list[str]
```

### DispatcherDecision（调度决策）

```python
class DispatcherDecision(BaseModel):
    route: str                        # "consult" | "explain" | "recommend" | "inventory" | "end"
    intent: str                       # "describe_symptom" | "ask_drug" | "switch_drug" | "switch_symptom" | "give_up" | "other"
    params: dict                      # {drug_name, reset_slots, filter_cheaper}
```

### SafetyResult（安全判定）

```python
class SafetyResult(BaseModel):
    verdict: str                      # "PASS" | "BLOCK" | "FILTER"
    triggered_rules: list[dict]       # [{rule_id, action, reason}]
    excluded_drugs: list[str]
    message: str
```

### DrugInfo（药品 — PostgreSQL）

```python
class Drug(Base):
    id: int (PK)
    generic_name: str                 # 通用名（布洛芬）
    brand_names: list[str]            # 商品名（芬必得、美林）
    category: str                     # 类别（解热镇痛类）
    active_ingredients: list[str]     # 活性成分
    dosage_form: str                  # 剂型（片剂/胶囊/颗粒）
    strength: str                     # 规格（0.2g/0.3g）
    otc_type: str                     # 甲类/乙类 OTC
    indication_summary: str           # 适应症简述
    usage_adult: str                  # 成人用法用量
    usage_child: str | None           # 儿童用法用量
    usage_elderly: str | None         # 老人用法用量
```

### Inventory（库存 — PostgreSQL）

```python
class Inventory(Base):
    id: int (PK)
    drug_id: int (FK → Drug)
    product_name: str                 # 商品名 + 厂家
    manufacturer: str                 # 生产厂家
    specification: str                # 规格包装
    stock_quantity: int               # 库存数量
    price: Decimal                    # 零售价（元）
    shelf_location: str               # 货架位置
    is_available: bool                # 是否在售
```

## 模块设计

### 模块 A: `api` — HTTP 接口层

**职责**：接收请求、参数校验、流式响应、异常转义

**端点**：

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/v1/chat/{session_id}` | POST | 发送消息，SSE 流式返回 |
| `/api/v1/sessions` | POST | 创建匿名会话 |
| `/api/v1/sessions/{session_id}` | GET | 查询会话状态和历史 |
| `/health` | GET | 健康检查（DB + Milvus + LLM 连通性） |

**SSE 事件类型**：

```
event: node      → {"node": "dispatcher", "route": "consult"}
event: token     → {"content": "您"}  ... {"content": "好"}
event: data      → {"phase": "recommending", "recommendations": [...]}
event: safety    → {"verdict": "PASS", "triggered_rules": [...]}
event: done      → {"session_id": "...", "usage": {"tokens": 150}}
event: error     → {"code": "SAFETY_BLOCK", "message": "..."}
```

**依赖**：`graph`、`db`

### 模块 B: `graph` — LangGraph 状态图

**职责**：定义节点、条件边、状态流转。编排一次完整 Graph Run。

**节点**：

| 节点 | 说明 |
|------|------|
| `intake_node` | 提取消息，更新 messages |
| `dispatcher_node` | LLM 综合分析上下文+意图，输出 DispatcherDecision |
| `consult_node` | ReAct Agent 追问，更新 consult_slots |
| `safety_check_node` | 调用规则引擎，输出 SafetyResult |
| `recommend_node` | 匹配药品，LLM 生成推荐理由，规则过滤 |
| `explain_node` | RAG 检索 + LLM 格式化 |
| `inventory_node` | 查 DB 库存，格式化输出 |
| `end_node` | 收尾：记录日志、标记会话状态 |

**条件边**：

- dispatcher_node → route 决定下一步
- consult_node → slots 充分 → safety_check_node；否则 END 等用户
- safety_check_node → PASS → recommend_node；BLOCK → end_node；FILTER → recommend_node
- recommend_node → inventory_node → end_node

**对外接口**：`run_graph(state) → AsyncGenerator[StreamEvent]`

**依赖**：`agent`、`rules`、`rag`、`db`、`llm`

### 模块 C: `agent` — ReAct 问诊代理

**职责**：Consult 节点的核心逻辑

**接口**：
```python
async def run_consult(state: ConversationState) -> dict:
    # 返回: {consult_slots, response, next_action: "ask"|"done", summary}
```

单次 LLM + 结构化输出。追问维度：症状细节、时间线、已服药、特殊人群、慢性病史、过敏史、其他症状。

**依赖**：`llm`

### 模块 D: `rules` — 规则引擎

**职责**：确定性安全检查，不调用任何 AI

**接口**：
```python
class RuleEngine:
    def register(self, rule: SafetyRule) -> None
    def check(self, slots: ConsultSlots, drugs: list[Drug] | None = None) -> SafetyResult

class SafetyRule(ABC):
    rule_id: str
    @abstractmethod
    def evaluate(self, slots: ConsultSlots) -> RuleResult
```

**执行流程**：先跑 BLOCK 规则（任一触发短路）→ 再跑 FILTER 规则

**MVP 规则**：R1~R7，每个规则一个文件

**依赖**：无外部模块依赖

### 模块 E: `rag` — 向量检索

**职责**：药品说明书嵌入、存储、检索。仅在 Explain 节点调用。

**接口**：
```python
class DrugManualRetriever:
    async def ingest(self, documents: list[Document]) -> None
    async def retrieve(self, drug_name: str, query: str, top_k: int = 5) -> list[Chunk]
```

**依赖**：`llm`（嵌入模型）

### 模块 F: `db` — 数据访问

**职责**：PostgreSQL 数据库模型与 CRUD

**Repository 类**：DrugRepository、InventoryRepository、SessionRepository、SafetyLogRepository

**数据库表**：`drugs`、`inventory`、`sessions`、`messages`、`safety_logs`

**依赖**：`sqlalchemy[asyncio]` + `asyncpg`

### 模块 G: `llm` — LLM 调用封装

**职责**：统一的 LLM 调用入口，OpenAI 兼容协议

**接口**：
```python
class LLMClient:
    async def generate(self, messages, **kwargs) -> ChatCompletion
    async def generate_structured(self, messages, schema: type[BaseModel], **kwargs) -> BaseModel
    def stream(self, messages, **kwargs) -> AsyncGenerator[str, None]
    async def embed(self, texts: list[str]) -> list[list[float]]
```

**依赖**：`openai` SDK、LangSmith callback

## 模块交互

### 主流程：问诊 → 推荐

```
User → API → intake → dispatcher → consult(追问) → END(等用户)
  ...多轮追问...
User → API → intake → dispatcher → consult(充分) → safety_check → recommend → inventory → end
```

### 话题跳转：中途问药

```
User("布洛芬副作用?") → dispatcher(route="explain", previous_phase="consulting")
  → explain(RAG检索+LLM格式化) → END
  → 下轮自动回归 Consult
```

### 依赖关系

```
api → graph → agent → llm
       │     → rules
       │     → rag → llm
       │     → db
api → db
```

## 文件组织

```
drug-agent/
├── app/
│   ├── __init__.py
│   ├── main.py                    # FastAPI 应用入口、lifespan、CORS
│   ├── config.py                  # Settings (pydantic-settings, .env)
│   │
│   ├── api/
│   │   ├── __init__.py
│   │   ├── routes/
│   │   │   ├── __init__.py
│   │   │   ├── chat.py            # POST /api/v1/chat/{session_id} (SSE)
│   │   │   ├── session.py         # POST/GET /api/v1/sessions
│   │   │   └── health.py          # GET /health
│   │   └── schemas.py             # Request/Response Pydantic 模型
│   │
│   ├── graph/
│   │   ├── __init__.py
│   │   ├── state.py               # ConversationState TypedDict 及其子结构
│   │   ├── builder.py             # build_graph() → CompiledStateGraph
│   │   ├── router.py              # 条件边函数
│   │   └── nodes/
│   │       ├── __init__.py
│   │       ├── intake.py
│   │       ├── dispatcher.py
│   │       ├── consult.py
│   │       ├── safety_check.py
│   │       ├── recommend.py
│   │       ├── explain.py
│   │       ├── inventory.py
│   │       └── end.py
│   │
│   ├── agent/
│   │   ├── __init__.py
│   │   ├── consult_agent.py       # ReAct 问诊代理
│   │   └── prompts.py             # System prompts
│   │
│   ├── rules/
│   │   ├── __init__.py
│   │   ├── engine.py              # RuleEngine
│   │   ├── base.py                # SafetyRule ABC + RuleResult
│   │   └── definitions/
│   │       ├── __init__.py
│   │       ├── r1_high_fever.py
│   │       ├── r2_infant_fever.py
│   │       ├── r3_pregnant_fever.py
│   │       ├── r4_emergency_signs.py
│   │       ├── r5_severe_allergy.py
│   │       ├── r6_drug_allergy.py
│   │       └── r7_child_aspirin.py
│   │
│   ├── rag/
│   │   ├── __init__.py
│   │   ├── retriever.py           # DrugManualRetriever
│   │   ├── ingestor.py            # 文档摄入
│   │   └── schemas.py             # Document, Chunk
│   │
│   ├── db/
│   │   ├── __init__.py
│   │   ├── database.py            # AsyncEngine, get_db_session
│   │   ├── models.py              # ORM: Drug, Inventory, Session, Message, SafetyLog
│   │   └── repositories/
│   │       ├── __init__.py
│   │       ├── drug.py
│   │       ├── inventory.py
│   │       ├── session.py
│   │       └── safety_log.py
│   │
│   └── llm/
│       ├── __init__.py
│       └── client.py              # LLMClient
│
├── data/
│   ├── drugs.json                 # 种子数据
│   ├── inventory.json             # 库存种子数据
│   └── rag_docs/                  # 药品说明书 (.txt)
│
├── tests/
│   ├── conftest.py
│   ├── unit/
│   │   ├── test_rules_engine.py
│   │   ├── test_dispatcher.py
│   │   └── test_consult_agent.py
│   └── integration/
│       ├── test_chat_flow.py
│       ├── test_safety_flow.py
│       └── test_rag.py
│
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .env.example
└── README.md
```

## 技术决策

| 决策点 | 选择 | 理由 |
|--------|------|------|
| LLM 接入协议 | OpenAI 兼容协议 | spec 要求 LLM 切换零改动；DashScope 提供 OpenAI 兼容端点 |
| 结构化输出 | Pydantic + `response_format: json_object`，兜底 tool_calling | Qwen 模型支持 JSON mode；Pydantic 做校验 |
| Graph 多轮机制 | 每个用户消息触发一次 Graph Run | 天然支持话题插断；Consult 跨 Run 循环 |
| Consult ReAct 实现 | 单次 LLM + 结构化输出 | MVP 避免多步 tool calling 延迟；单次调用 < 2s |
| 规则引擎架构 | 抽象基类 + 插件式注册 + 两阶段执行 | spec N3 可扩展性；BLOCK 短路 |
| RAG 框架 | 自建（Milvus SDK + text-splitters） | 仅检索场景无需 LangChain RAG 全家桶 |
| 流式传输 | LangGraph astream_events + SSE | 原生流式；按 event 类型渲染 |
| 数据库异步驱动 | asyncpg + SQLAlchemy 2.0 async | FastAPI 全异步链路 |
| 数据库迁移 | Alembic | 版本化迁移，MVP 后表结构会变化 |
| Milvus 部署 | Milvus Standalone (Docker Compose) | 单机够用，一键启动 |
| 嵌入模型 | DashScope text-embedding-v3 | 与 LLM 同厂商，中文药品说明书质量好 |
| LangSmith | LANGCHAIN_TRACING_V2 + API Key | LangGraph 原生集成，零代码 |
| 配置管理 | pydantic-settings + .env | 类型安全；.env.example 作文档 |
| 依赖注入 | 手动注入（lifespan 初始化 → graph builder） | MVP 规模不需要 DI 框架 |
| 单元测试 | pytest + 参数化 + mock LLM | 规则引擎纯函数；Graph 流程 mock LLM |
