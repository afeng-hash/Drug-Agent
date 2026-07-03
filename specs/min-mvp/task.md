# 感冒退烧 OTC AI 导购系统 Tasks

## 文件清单

| 操作 | 文件 | 职责 |
|------|------|------|
| 新建 | `requirements.txt` | Python 依赖 |
| 新建 | `docker-compose.yml` | PostgreSQL 17 + Milvus Standalone |
| 新建 | `.env.example` | 环境变量模板 |
| 新建 | `app/__init__.py` | 空 |
| 新建 | `app/config.py` | pydantic-settings 配置 |
| 新建 | `app/main.py` | FastAPI 入口、lifespan、CORS |
| 新建 | `app/llm/__init__.py` | 空 |
| 新建 | `app/llm/client.py` | LLMClient (OpenAI SDK) |
| 新建 | `app/db/__init__.py` | 空 |
| 新建 | `app/db/database.py` | AsyncEngine、get_db_session、init_db |
| 新建 | `app/db/models.py` | Drug、Inventory、Session、Message、SafetyLog ORM |
| 新建 | `app/db/repositories/__init__.py` | 空 |
| 新建 | `app/db/repositories/drug.py` | DrugRepository |
| 新建 | `app/db/repositories/inventory.py` | InventoryRepository |
| 新建 | `app/db/repositories/session.py` | SessionRepository |
| 新建 | `app/db/repositories/safety_log.py` | SafetyLogRepository |
| 新建 | `app/rules/__init__.py` | 空 |
| 新建 | `app/rules/base.py` | SafetyRule ABC、RuleResult |
| 新建 | `app/rules/engine.py` | RuleEngine (注册、两阶段执行) |
| 新建 | `app/rules/definitions/__init__.py` | register_all_rules 工厂函数 |
| 新建 | `app/rules/definitions/r1_high_fever.py` | R1 高热规则 |
| 新建 | `app/rules/definitions/r2_infant_fever.py` | R2 婴儿发热规则 |
| 新建 | `app/rules/definitions/r3_pregnant_fever.py` | R3 孕妇发热规则 |
| 新建 | `app/rules/definitions/r4_emergency_signs.py` | R4 急症信号规则 |
| 新建 | `app/rules/definitions/r5_severe_allergy.py` | R5 严重过敏规则 |
| 新建 | `app/rules/definitions/r6_drug_allergy.py` | R6 药物过敏规则 |
| 新建 | `app/rules/definitions/r7_child_aspirin.py` | R7 儿童阿司匹林规则 |
| 新建 | `app/rag/__init__.py` | 空 |
| 新建 | `app/rag/schemas.py` | Document、Chunk |
| 新建 | `app/rag/ingestor.py` | 文档加载→分块→嵌入→存储 |
| 新建 | `app/rag/retriever.py` | DrugManualRetriever |
| 新建 | `app/agent/__init__.py` | 空 |
| 新建 | `app/agent/prompts.py` | Dispatcher、Consult、Explain System Prompts |
| 新建 | `app/agent/consult_agent.py` | run_consult (ReAct 问诊) |
| 新建 | `app/graph/__init__.py` | 空 |
| 新建 | `app/graph/state.py` | ConversationState 定义 |
| 新建 | `app/graph/builder.py` | build_graph → CompiledStateGraph |
| 新建 | `app/graph/router.py` | 条件边函数 |
| 新建 | `app/graph/nodes/__init__.py` | 空 |
| 新建 | `app/graph/nodes/intake.py` | intake_node |
| 新建 | `app/graph/nodes/dispatcher.py` | dispatcher_node |
| 新建 | `app/graph/nodes/consult.py` | consult_node |
| 新建 | `app/graph/nodes/safety_check.py` | safety_check_node |
| 新建 | `app/graph/nodes/recommend.py` | recommend_node |
| 新建 | `app/graph/nodes/explain.py` | explain_node |
| 新建 | `app/graph/nodes/inventory.py` | inventory_node |
| 新建 | `app/graph/nodes/end.py` | end_node |
| 新建 | `app/api/__init__.py` | 空 |
| 新建 | `app/api/schemas.py` | Request/Response Pydantic 模型 |
| 新建 | `app/api/routes/__init__.py` | 空 |
| 新建 | `app/api/routes/health.py` | GET /health |
| 新建 | `app/api/routes/session.py` | POST/GET /api/v1/sessions |
| 新建 | `app/api/routes/chat.py` | POST /api/v1/chat/{session_id} (SSE) |
| 新建 | `data/drugs.json` | 10-15 种感冒退烧药品种子数据 |
| 新建 | `data/inventory.json` | 库存种子数据 |
| 新建 | `data/rag_docs/*.txt` | 药品说明书全文（≥5 种药） |
| 新建 | `data/seed.py` | 种子数据导入脚本 |
| 新建 | `tests/__init__.py` | 空 |
| 新建 | `tests/conftest.py` | 共享 fixtures |
| 新建 | `tests/unit/__init__.py` | 空 |
| 新建 | `tests/unit/test_rules_engine.py` | 规则引擎参数化测试 |
| 新建 | `tests/unit/test_dispatcher.py` | Dispatcher 路由测试 |
| 新建 | `tests/unit/test_consult_agent.py` | Consult Agent 测试 |
| 新建 | `tests/integration/__init__.py` | 空 |
| 新建 | `tests/integration/test_chat_flow.py` | 主流程集成测试 |
| 新建 | `tests/integration/test_safety_flow.py` | 安全阻断流程测试 |
| 新建 | `tests/integration/test_rag.py` | RAG 检索测试 |

共 **56 个文件**。

---

## T1: 项目骨架 + 基础设施配置

**文件**：`requirements.txt`、`docker-compose.yml`、`.env.example`、`app/__init__.py`、`app/config.py`

**依赖**：无

**步骤**：
1. 创建 `requirements.txt`：fastapi、uvicorn、langgraph、langchain-core、openai、sqlalchemy[asyncio]、asyncpg、pymilvus、pydantic-settings、python-dotenv、langsmith、pytest、pytest-asyncio
2. 创建 `docker-compose.yml`：PostgreSQL 17（端口 5432）+ Milvus Standalone（端口 19530）+ etcd + minio
3. 创建 `.env.example`：LLM_BASE_URL、LLM_API_KEY、LLM_MODEL、EMBEDDING_MODEL、DATABASE_URL、MILVUS_HOST、MILVUS_PORT、LANGSMITH_API_KEY
4. 创建 `app/config.py`：用 pydantic-settings 定义 Settings 类，读取所有环境变量

**验证**：`docker-compose up -d` → `docker ps` 显示 postgres 和 milvus 容器运行中

---

## T2: LLM 客户端

**文件**：`app/llm/__init__.py`、`app/llm/client.py`

**依赖**：T1（config）

**步骤**：
1. 实现 `LLMClient.__init__`：接收 base_url、api_key、model，初始化 `openai.AsyncOpenAI`
2. 实现 `generate()`：调用 `chat.completions.create`，返回完整 ChatCompletion
3. 实现 `generate_structured()`：调用 generate，用 `response_format={"type": "json_object"}`，解析为 Pydantic 模型；若模型不支持则 fallback 到 tool_calling
4. 实现 `stream()`：调用 `chat.completions.create(stream=True)`，返回 AsyncGenerator[str]
5. 实现 `embed()`：调用 `embeddings.create`，返回 embedding 向量列表

**验证**：创建临时脚本，mock base_url 指向本地 echo server，验证各方法签名和调用链路正确

---

## T3: 数据库模型 + 连接管理

**文件**：`app/db/__init__.py`、`app/db/database.py`、`app/db/models.py`

**依赖**：T1（config）

**步骤**：
1. 实现 `database.py`：创建 `async_engine`（从 Settings 读取 DATABASE_URL）、`async_sessionmaker`、`get_db()` 依赖生成器、`init_db()` 建表函数
2. 实现 `models.py`：定义 5 个 SQLAlchemy ORM 模型
   - `Drug`：id、generic_name、brand_names(JSON)、category、active_ingredients(JSON)、dosage_form、strength、otc_type、indication_summary、usage_adult、usage_child、usage_elderly
   - `Inventory`：id、drug_id(FK)、product_name、manufacturer、specification、stock_quantity、price、shelf_location、is_available
   - `Session`：id、session_id(UUID, unique)、status、expires_at、created_at、updated_at
   - `Message`：id、session_id(FK)、role、content、intent、metadata(JSON)、created_at
   - `SafetyLog`：id、session_id(FK)、verdict、triggered_rules(JSON)、input_slots(JSON)、created_at

**验证**：`docker-compose up -d` → 运行 `init_db()` → `psql` 连接查看 5 张表已创建

---

## T4: 数据仓库层

**文件**：`app/db/repositories/__init__.py`、`drug.py`、`inventory.py`、`session.py`、`safety_log.py`

**依赖**：T3（models + database）

**步骤**：
1. `DrugRepository`：实现 `find_by_symptoms(symptoms, category)` — 用 ILIKE 模糊匹配 indication_summary；`find_by_name(generic_name)`；`list_all()`
2. `InventoryRepository`：实现 `find_by_drug(drug_id)`；`find_by_drugs(drug_ids)`
3. `SessionRepository`：实现 `create()` — 生成 UUID、设置 30 分钟过期；`get(session_id)` — 检查过期自动关闭；`close(session_id)`；`add_message(session_id, message)`
4. `SafetyLogRepository`：实现 `log(session_id, result)` — 记录安全判定

**验证**：用测试数据库 session，插入一条 Drug 记录 → `find_by_name` 查回 → 用 `find_by_symptoms(["头痛", "发热"])` 查回

---

## T5: 规则引擎基类 + 引擎

**文件**：`app/rules/__init__.py`、`app/rules/base.py`、`app/rules/engine.py`

**依赖**：无

**步骤**：
1. 定义 `RuleResult` dataclass：triggered(bool)、action("BLOCK"|"FILTER"|"NONE")、reason(str)、excluded_drugs(list[str])
2. 定义 `SafetyRule` ABC：rule_id、description 属性、`evaluate(slots: dict) → RuleResult` 抽象方法
3. 实现 `RuleEngine`：
   - `register(rule)` — 注册规则
   - `check(slots, drugs=None)` — 两阶段执行：先跑所有 BLOCK 规则（任一触发短路返回），再跑 FILTER 规则（聚合 excluded_drugs）
   - 构建 `SafetyResult(verdict, triggered_rules, excluded_drugs, message)`
   - message 生成：BLOCK 时汇总就医理由，PASS 时为空

**验证**：创建两个 mock 规则（一个 BLOCK、一个 FILTER）→ `engine.register` → `engine.check` → 断言 BLOCK 触发后 FILTER 未执行（短路）

---

## T6: 7 条安全规则实现

**文件**：`app/rules/definitions/__init__.py`、`r1_high_fever.py` ~ `r7_child_aspirin.py`

**依赖**：T5（base + engine）

**步骤**：
1. `R1_HighFever`：slots.temperature >= 39.0 AND slots.duration_days >= 3 → BLOCK，理由"持续高热，建议立即就医"
2. `R2_InfantFever`：slots.age < 0.25（3个月）AND slots.temperature > 0 → BLOCK
3. `R3_PregnantFever`：slots.special_population == "pregnant" AND slots.temperature >= 38.5 → BLOCK
4. `R4_EmergencySigns`：slots.other_symptoms 包含"呼吸困难/胸痛/意识模糊"任一关键词 → BLOCK
5. `R5_SevereAllergy`：slots.other_symptoms 包含"皮疹/全身过敏"关键词 → BLOCK
6. `R6_DrugAllergy`：slots.allergies 非空 → 在 drugs 列表中匹配成分 → FILTER + excluded_drugs
7. `R7_ChildAspirin`：slots.age < 12 → 在 drugs 列表中排除含阿司匹林的 → FILTER
8. `definitions/__init__.py`：`register_all_rules(engine)` 工厂函数，注册全部 7 条

**验证**：`pytest tests/unit/test_rules_engine.py`（T10 实现后跑），每条规则参数化覆盖触发/不触发/边界条件

---

## T7: RAG 检索器

**文件**：`app/rag/__init__.py`、`app/rag/schemas.py`、`app/rag/retriever.py`

**依赖**：T2（llm embed）、T1（config milvus）

**步骤**：
1. 定义 `Document` 和 `Chunk` pydantic 模型
2. 实现 `DrugManualRetriever.__init__`：连接 Milvus（pymilvus），创建/获取 collection `drug_manuals`（768 维向量 + drug_id、section、content、drug_name 标量字段）
3. 实现 `retrieve(drug_name, query, top_k=5)`：
   - 用 llm.embed([query]) 生成查询向量
   - 调用 milvus.search（过滤 `drug_name == drug_name`）
   - 返回 top_k 个 Chunk

**验证**：手动向 Milvus 插入一条测试数据 → `retrieve("布洛芬", "副作用")` → 返回该条数据

---

## T8: RAG 文档摄入

**文件**：`app/rag/ingestor.py`

**依赖**：T7（retriever, schemas）

**步骤**：
1. 实现 `ingest_documents(data_dir: str)`：
   - 遍历 `data/rag_docs/` 下 `.txt` 文件
   - 文件名解析 drug_name（如 `布洛芬.txt` → "布洛芬"）
   - 用 `langchain.text_splitter.RecursiveCharacterTextSplitter`（chunk_size=500, overlap=50）分块
   - 每块打标签：drug_name、section（按标题匹配：不良反应/禁忌/注意事项/药物相互作用，其余标"通用"）
   - 批量调用 llm.embed() 生成向量
   - 插入 Milvus

**验证**：准备 2 个测试 .txt → 运行 `ingest_documents` → `retrieve` 能搜到对应药品的片段

---

## T9: System Prompts

**文件**：`app/agent/__init__.py`、`app/agent/prompts.py`

**依赖**：无（纯文本）

**步骤**：
1. `DISPATCHER_PROMPT`：定义对话调度器 System Prompt
   - 输入：历史消息摘要 + 当前 slots 摘要 + 当前用户消息
   - 输出：route（目标节点）、intent（用户意图）、params
   - 路由规则：描述症状→consult、问药→explain、换药→recommend、放弃→end、闲聊→end
   - 关键：如果当前处于 consulting 且用户问药，设置 previous_phase 用于回归
2. `CONSULT_PROMPT`：定义问诊 Agent System Prompt
   - 角色：OTC 药店问诊助手
   - 追问维度：症状细节、时间线、已服药、特殊人群、慢性病史、过敏史、其他症状
   - 规则：一次 1-2 问、不重复已填维度、尊重用户不耐烦、充分标准（主要症状+持续时间+特殊人群状态）
   - 输出：updated_slots、response、next_action(ask|done)、summary
3. `EXPLAIN_PROMPT`：定义药品解释 System Prompt
   - 输入：RAG 检索片段 + DB 药品信息
   - 输出：结构化药品说明（药品名称、作用类别、适应症、用法用量、不良反应、禁忌、药物相互作用、注意事项）

**验证**：Python 脚本打印 prompt 文本，人工检查是否有歧义或遗漏

---

## T10: Consult Agent

**文件**：`app/agent/consult_agent.py`

**依赖**：T2（llm）、T9（prompts）

**步骤**：
1. 实现 `run_consult(state: ConversationState) → dict`：
   - 从 state 提取 messages、consult_slots
   - 构建 messages = [System(CONSULT_PROMPT), ...history, User(最新消息)]
   - 调用 `llm.generate_structured(messages, ConsultResultSchema)`
   - 解析返回：updated_slots、response、next_action、summary
   - 校验：updated_slots 不能丢失已有字段（merge 逻辑）
   - 最大追问轮数检查：如果已追问道 6 轮，强制 next_action="done"

**验证**：mock LLM 返回固定 JSON → `run_consult` → 断言返回值结构正确

---

## T11: Graph State + 基础节点 (Intake + Dispatcher)

**文件**：`app/graph/__init__.py`、`app/graph/state.py`、`app/graph/nodes/__init__.py`、`intake.py`、`dispatcher.py`

**依赖**：T2（llm）、T9（prompts）

**步骤**：
1. `state.py`：定义 `ConversationState` TypedDict，包含 plan.md 中所有字段及默认值
2. `intake.py`：实现 `intake_node(state)` — 提取最新 user message→ 追加到 messages → 返回更新
3. `dispatcher.py`：实现 `dispatcher_node(state)` — 构建调度上下文（messages 摘要 + consult_slots 摘要 + 当前消息）→ `llm.generate_structured(DISPATCHER_PROMPT, DispatcherDecision)` → 更新 state.dispatcher_result 和 state.previous_phase

**验证**：mock LLM 返回 route="consult" → 运行 intake → dispatcher → 断言 state.dispatcher_result.route == "consult"

---

## T12: Consult 节点

**文件**：`app/graph/nodes/consult.py`

**依赖**：T10（consult_agent）、T11（state）

**步骤**：
1. 实现 `consult_node(state)`：
   - 调用 `run_consult(state)`
   - 更新 `state.consult_slots`
   - 设置 `state.response`
   - 如果 next_action="done"：设置标记让 router 进入 safety_check；并保存 summary 到 state
   - 如果 next_action="ask"：设置 `state.phase="consulting"`
   - 返回 state 更新 + response 用于 SSE 流式输出

**验证**：构造 state（含 2 轮病史 + slots 已有 4 个维度）→ mock LLM 返回 next_action="done" → 断言 router 导向 safety_check

---

## T13: SafetyCheck 节点

**文件**：`app/graph/nodes/safety_check.py`

**依赖**：T6（rules）、T11（state）

**步骤**：
1. 实现 `safety_check_node(state)`：
   - 从 state 获取 `rule_engine` 实例（通过闭包注入）
   - 调用 `rule_engine.check(state.consult_slots, state.candidate_drugs)`
   - 更新 `state.safety_result`
   - 如果 BLOCK：设置 `state.response` 为就医引导文案、`state.phase="ended"`
   - 如果 PASS/FILTER：记录结果，继续路由

**验证**：构造 slots（体温 39.5°C, duration=4天）→ 运行节点 → 断言 safety_result.verdict == "BLOCK"

---

## T14: Recommend 节点

**文件**：`app/graph/nodes/recommend.py`

**依赖**：T4（DrugRepository）、T2（llm）、T13（SafetyCheck 通过后路由到此）

**步骤**：
1. 实现 `recommend_node(state)`：
   - 从 state.consult_slots 提取症状 summary
   - 调用 `DrugRepository.find_by_symptoms(symptoms, category="感冒退烧")`
   - 如果 SafetyCheck 结果为 FILTER，排除 excluded_drugs
   - 调用 LLM 生成推荐理由和排序（structured output：list[{drug_id, match_reason, score}]）
   - 设置 `state.recommendations`（1-3 个）
   - 设置 `state.response`（推荐文案 + 免责声明）
   - 设置 `state.phase="recommending"`

**验证**：构造 slots（头痛发热 2 天、成人、无过敏）→ mock DrugRepository 返回 5 个药 → mock LLM 返回排序 → 断言 state.recommendations 长度 ≤3

---

## T15: Explain 节点

**文件**：`app/graph/nodes/explain.py`

**依赖**：T7（retriever）、T4（DrugRepository）、T2（llm）、T9（EXPLAIN_PROMPT）

**步骤**：
1. 实现 `explain_node(state)`：
   - 从 `state.dispatcher_result.params.drug_name` 获取药名
   - 调用 `DrugRepository.find_by_name(drug_name)` 获取 DB 结构数据
   - 调用 `DrugManualRetriever.retrieve(drug_name, "不良反应 禁忌 用法 注意事项")`
   - 拼接 RAG 片段 + DB 数据 → LLM 格式化
   - 设置 `state.response`（结构化药品说明）
   - 记录 `state.previous_phase` 用于下一轮回归

**验证**：构造 params={drug_name: "布洛芬"} → mock retriever 返回 3 个片段 → mock LLM 格式化 → 断言 response 包含"布洛芬"

---

## T16: Inventory + End 节点

**文件**：`app/graph/nodes/inventory.py`、`end.py`

**依赖**：T4（InventoryRepository、SessionRepository、SafetyLogRepository）

**步骤**：
1. `inventory_node(state)`：
   - 从 `state.recommendations` 提取 drug_ids
   - 调用 `InventoryRepository.find_by_drugs(drug_ids)`
   - 格式化输出：每个推荐药品的库存状态、价格、位置
   - 若无货，调用 `DrugRepository.find_alternatives` 找替代药
   - 设置 `state.response`
2. `end_node(state)`：
   - 调用 `SessionRepository.add_message` 保存用户消息和 AI 回复
   - 如果有 safety_result 非空，调用 `SafetyLogRepository.log`
   - 更新 session updated_at

**验证**：构造 recommendations（含 2 个 drug_id）→ mock InventoryRepository 返回库存信息 → 断言 response 包含价格和位置

---

## T17: Graph Router + Builder

**文件**：`app/graph/router.py`、`app/graph/builder.py`

**依赖**：T11-T16（所有节点）

**步骤**：
1. `router.py`：
   - `route_after_dispatcher(state)`：读取 `state.dispatcher_result.route`，返回对应节点名
   - `route_after_consult(state)`：如果 `next_action=="done"` → `"safety_check"`；否则 → `END`
   - `route_after_safety(state)`：如果 `verdict=="BLOCK"` → `"end"`；否则 → `"recommend"`
2. `builder.py`：
   - 实现 `build_graph(llm_client, rule_engine, drug_repo, ...)`：
     - 创建 `StateGraph(ConversationState)`
     - 添加 8 个节点
     - 添加边：intake → dispatcher
     - 添加条件边：dispatcher → route_after_dispatcher
     - 添加条件边：consult → route_after_consult
     - 添加条件边：safety_check → route_after_safety
     - 添加普通边：recommend → inventory → end
     - 编译返回 `CompiledStateGraph`
   - 节点函数通过闭包捕获外部依赖

**验证**：`build_graph(...)` 不抛异常 → 打印 `graph.get_graph().draw_mermaid()` 查看拓扑

---

## T18: API Schemas + Health 端点

**文件**：`app/api/__init__.py`、`app/api/schemas.py`、`app/api/routes/__init__.py`、`health.py`

**依赖**：T1（config）

**步骤**：
1. `schemas.py`：定义
   - `ChatRequest(message: str)`
   - `SessionResponse(session_id: str, status: str, created_at: datetime)`
   - `HealthResponse(status: str, postgres: str, milvus: str, llm: str)`
2. `health.py`：
   - `GET /health` → 检查 PostgreSQL 连接（`SELECT 1`）、Milvus 连接（`list_collections`）、LLM API Key 非空
   - 返回 `HealthResponse`

**验证**：`curl http://localhost:8000/health` → 返回 JSON 含各组件状态

---

## T19: Session API

**文件**：`app/api/routes/session.py`

**依赖**：T4（SessionRepository）、T18（schemas）

**步骤**：
1. `POST /api/v1/sessions` → `SessionRepository.create()` → 返回 `SessionResponse(session_id, status="active")`
2. `GET /api/v1/sessions/{session_id}` → `SessionRepository.get()` → 返回会话状态 + 消息历史列表

**验证**：POST → 拿到 session_id → GET 确认 status=active → 等待 30 分钟 → GET 确认 status=expired

---

## T20: Chat SSE 端点

**文件**：`app/api/routes/chat.py`

**依赖**：T17（graph builder）、T19（session）、T18（schemas）、T4（SessionRepository）

**步骤**：
1. `POST /api/v1/chat/{session_id}`：
   - 校验 session 存在且 active
   - 获取对话历史，构建初始 `ConversationState`
   - 调用 `graph.astream_events(state)` → 映射到 SSE 事件流
   - SSE 流格式：
     ```
     event: node      data: {"node":"dispatcher","route":"consult"}
     event: token     data: {"content":"您好..."}
     event: data      data: {"phase":"recommending","recommendations":[...]}
     event: safety    data: {"verdict":"PASS",...}
     event: done      data: {"session_id":"...","usage":{...}}
     ```
   - 异常处理：SafetyCheck BLOCK → event:safety 后 event:done；LLM 超时 → event:error
2. 实现 token 流式转发：LLM stream() 的每个 token → SSE "token" event

**验证**：`curl -N POST /api/v1/chat/xxx -d '{"message":"我头疼"}'` → 看到 SSE 事件逐条输出

---

## T21: FastAPI 主入口

**文件**：`app/main.py`

**依赖**：T20（chat）、T19（session）、T18（health）、T17（graph）、T1（config）

**步骤**：
1. 实现 `lifespan(app)`：
   - Startup：初始化 Settings → 创建 DB 连接池 → init_db() → 创建 LLMClient → 创建 Milvus 连接 → 创建 RuleEngine + 注册规则 → 创建所有 Repository → build_graph() → 存储到 app.state
   - Shutdown：关闭 DB 连接池、Milvus 连接
2. 创建 FastAPI app，挂载 lifespan
3. 添加 CORS 中间件（允许所有来源，开发阶段）
4. 注册路由：health_router、session_router、chat_router
5. `__main__` 入口：`uvicorn.run("app.main:app", reload=True)`

**验证**：`uvicorn app.main:app` → 启动无报错 → `/health` 返回正常 → `/docs` 显示 Swagger UI

---

## T22: 种子数据 + 导入脚本

**文件**：`data/drugs.json`、`data/inventory.json`、`data/rag_docs/*.txt`、`data/seed.py`

**依赖**：T3（models）、T4（repos）、T8（RAG ingestor）

**步骤**：
1. `data/drugs.json`：10-15 种感冒退烧药，按 Drug 模型字段组织（布洛芬、对乙酰氨基酚、复方氨酚烷胺、酚麻美敏、伪麻黄碱、右美沙芬、氯苯那敏、金刚烷胺、连花清瘟、板蓝根等）
2. `data/inventory.json`：每种药 1-3 个在售商品（不同厂家/规格），含价格和货架位置
3. `data/rag_docs/*.txt`：至少 5 种核心药的说明书全文（布洛芬、对乙酰氨基酚、复方氨酚烷胺、酚麻美敏、伪麻黄碱）
4. `data/seed.py`：
   - 读取 drugs.json → 批量 insert Drug
   - 读取 inventory.json → 批量 insert Inventory
   - 调用 `DrugManualRetriever.ingest(rag_docs/)` 向量化存储
   - 命令行：`python data/seed.py`

**验证**：`python data/seed.py` → 无报错 → `psql` 查询 drugs 表有 10+ 行 → Milvus 查询有向量数据

---

## 执行顺序

```
T1 ──→ T2 ──→ T3 ──→ T4
  │       │              │
  │       └──→ T7 ──→ T8 │
  │                      │
  ├──→ T5 ──→ T6          │
  │                      │
  └──→ T9 ──→ T10        │
              │           │
              ▼           │
        T11 ──→ T12 ──→ T13 ──→ T14 ──→ T15 ──→ T16
                                          │
                                          ▼
                                        T17 ──→ T18 ──→ T19 ──→ T20 ──→ T21
                                                                          │
                                                                          ▼
                                                                        T22
```

并行点：
- T5-T6（规则引擎）可与 T3-T4（数据库）并行
- T7-T8（RAG）可与 T5-T6（规则）并行
- T9-T10（Agent）可与 T5-T8（规则+RAG）并行
