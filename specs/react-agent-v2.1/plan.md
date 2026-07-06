# ReactAgent v2.1 Plan

## 架构概览

```
                             ┌─────────────────┐
                             │   ReactAgent     │
                             │   (agent.py)     │
                             └────────┬────────┘
                                      │
                       ┌──────────────┼──────────────┐
                       │              │              │
                  ┌────▼────┐  ┌─────▼──────┐  ┌───▼──────────┐
                  │本地工具  │  │本地工具     │  │ 联网工具      │
                  │(DB/Milvus)│  │(StateProxy)│  │ search_web   │
                  └────┬────┘  └─────┬──────┘  └───┬──────────┘
                       │              │              │
                       │              │         ┌────▼──────────┐
                       │              │         │WebSearchService│
                       │              │         │ (Bing API)    │
                       │              │         └───────────────┘
                       ▼              ▼
                  ┌──────────────────────┐
                  │   MySQL / Milvus     │
                  └──────────────────────┘
```

三层数据源：
  Layer 1: 结构化 DB（search_drug, get_drug_detail）
  Layer 2: 向量检索 Milvus（search_manual）
  Layer 3: 联网搜索（search_web）——仅在前两层不足时触发

## 核心数据结构

### WebSearchResult
```python
class WebSearchResult(BaseModel):
    """单条网络搜索结果。"""
    title: str              # 结果标题
    snippet: str            # 摘要片段
    url: str                # 来源 URL
    source: str = "web"     # 数据来源标记，固定 "web"
```

### WebSearchResponse
```python
class WebSearchResponse(BaseModel):
    """网络搜索完整响应。"""
    query: str                          # 实际搜索 query
    results: list[WebSearchResult]       # 结果列表（最多 5 条）
    total_estimated: int                # 搜索引擎返回的总结果数
    source: str = "web"                 # 标记：来自网络
    warning: str = ""                   # 免责声明文本
```

### WebSearchService (Protocol)
```python
class WebSearchService(ABC):
    """网络搜索服务接口。可替换实现（Bing / SearXNG / ...）。"""
    
    @abstractmethod
    async def search(self, query: str, num_results: int = 5) -> WebSearchResponse: ...
    
    @property
    @abstractmethod
    def is_available(self) -> bool: ...
```

## 模块设计

### 模块 A: `app/search/` — 网络搜索服务（新建）

**职责**：封装网络搜索 API 调用，返回标准化结果
**对外接口**：`WebSearchService` 抽象类 + `BingSearchService` 实现
**依赖**：`httpx`（HTTP 客户端）、`app.config.Settings`

```
app/search/
  __init__.py           # 导出 WebSearchService, BingSearchService, WebSearchResult, WebSearchResponse
  service.py            # WebSearchService 抽象类 + BingSearchService 实现
  schemas.py            # WebSearchResult, WebSearchResponse
```

### 模块 B: `app/agent/react/tools/search_web.py` — 联网搜索工具（新建）

**职责**：将 WebSearchService 包装为 BaseTool，供 ReactAgent 调用
**对外接口**：`SearchWebTool(BaseTool)`
**依赖**：`WebSearchService`
**容错**：`fallback_tools = []`（这是最后一级，没有替代工具）
**能力标签**：`capabilities = ["web_search"]`

关键设计：
- 工具 description 明确告知 LLM：「仅在本地工具（search_manual、get_drug_detail）返回空或不充分时使用」
- 返回结果中每条都带 `source_url`，供 LLM 做来源标注

### 模块 C: `app/agent/react/agent.py` — 反编造增强（修改）

**职责**：在工具调用循环中增加空结果检测
**改动**：
- `_handle_tool_calls`：工具返回空列表 `[]` 时，包装为 `{"found": false, "results": [], "message": "本地知识库未找到相关信息"}`
- `_format_raw_result`：LLM 完全不可用时的降级文案去掉所有药品信息，改为纯服务不可用提示

### 模块 D: `app/agent/prompts.py` — Prompt 增强（修改）

**职责**：REACT_SYSTEM_PROMPT 增加以下内容
**改动**：
- 空结果行为：明确「空结果 ≠ 可以编造」，「必须如实说明」
- 联网搜索调用规则：何时触发、如何构造 query
- 来源标注格式：`📋 本地知识库` + `🌐 网络补充（附带来源链接+免责声明）`

### 模块 E: `app/graph/nodes/react.py` — Bug 修复（修改）

**职责**：修复两个 bug
**F1 消息顺序修复**：
- `normalize_messages(messages)` 后的结果同时传给 `react_agent.run(history=normalized)` 
- 移除 query 提取中不必要的 reversed 遍历——直接从 dispatcher_result.actions 中取 react query，只有极端 fallback 才遍历消息

**F2 状态一致性修复**：
```python
# 改前（有 bug）
workflow_action = "done" if consult_next_action == "done" else "ask"

# 改后
if recommendations and state.get("phase") in ("recommending", "ended"):
    workflow_action = "done"
elif consult_next_action == "ask":
    workflow_action = "ask"
else:
    workflow_action = "done"  # 默认当 done
```

### 模块 F: `app/graph/builder.py` — 工具注册（修改）

**职责**：在工具列表中增加 `SearchWebTool`
**改动**：两行
```python
web_search_service = BingSearchService(settings)
tools: list[BaseTool] = [
    ...
    SearchWebTool(web_search_service=web_search_service),  # ← 新增
]
```

### 模块 G: `app/config.py` — 配置扩展（修改）

**职责**：增加联网搜索相关配置
**新增字段**：
```python
web_search_enabled: bool = True     # 是否启用联网搜索
web_search_api_key: str = ""        # Bing Web Search API Key
web_search_endpoint: str = "https://api.bing.microsoft.com/v7.0/search"
web_search_timeout: float = 10.0    # 单次搜索超时（秒）
web_search_max_results: int = 5     # 最大返回结果数
```

## 模块交互

### 正常流程（本地数据充足）
```
User → react_node → ReactAgent.run()
  → LLM calls search_manual("布洛芬", "副作用")
  → Milvus returns [3 chunks]
  → LLM formats response from chunks
  → final_response (no web search triggered)
```

### 兜底流程（本地数据不足）
```
User → react_node → ReactAgent.run()
  → LLM calls search_manual("氢溴酸右美沙芬", "孕妇能用吗")
  → Milvus returns []  → wrapped as {"found": false, ...}
  → LLM calls get_drug_detail("氢溴酸右美沙芬")
  → DB returns drug but no pregnancy info → not sufficient
  → LLM determines: "本地工具均未返回有效数据"
  → LLM calls search_web("氢溴酸右美沙芬 孕妇 安全性 说明书")
  → Bing returns [3 web results with URLs]
  → LLM formats response:
    "📋 本地知识库：未找到相关信息。
     🌐 网络补充（以下信息来自互联网，仅供参考，请以药品说明书或医生意见为准）：
     - [title 1] — [snippet 1]（来源：url1）
     - [title 2] — [snippet 2]（来源：url2）"
```

### 联网搜索不可用
```
search_web → WebSearchService.is_available == False
  → tool returns {"found": false, "error": "联网搜索服务不可用"}
  → LLM: "抱歉，本地知识库和联网搜索均未找到相关信息。建议您查看药品纸质说明书或咨询药师。"
```

## 文件组织

```
app/
  search/
    __init__.py               # 新建 — 导出 WebSearchService, BingSearchService, WebSearchResult, WebSearchResponse
    service.py                # 新建 — WebSearchService 抽象 + BingSearchService 实现
    schemas.py                # 新建 — WebSearchResult, WebSearchResponse
  agent/
    react/
      tools/
        search_web.py         # 新建 — SearchWebTool
    prompts.py                # 修改 — REACT_SYSTEM_PROMPT 增强
    react/
      agent.py                # 修改 — _handle_tool_calls 空结果包装
  graph/
    nodes/
      react.py                # 修改 — F1 消息顺序 + F2 状态一致性
    builder.py                # 修改 — 增加 SearchWebTool 注册
  config.py                   # 修改 — 增加 web_search_* 配置

tests/
  unit/
    test_search_web_tool.py   # 新建 — SearchWebTool 单元测试
    test_react_agent.py       # 修改 — 空结果行为测试
    test_react.py             # 修改 — 状态一致性测试
```

## 技术决策

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 网络搜索后端 | Bing Web Search API v7 | 用户指定；有免费额度（1000次/月）；返回结构化 JSON |
| 空结果防御层级 | Prompt + 代码包装双重 | Prompt 层覆盖正常 LLM 行为；代码包装覆盖异常情况（LLM 忽略 prompt） |
| 工具结果空值包装 | `{"found": false}` 替代裸 `[]` | 裸 `[]` 对 LLM 语义模糊（可能被理解为"查询未执行"）；显式标记消除歧义 |
| 联网搜索触发策略 | LLM 自主判断（prompt 引导） | 比代码层硬编码更灵活；prompt 可精确描述触发条件；后续可调整 |
| 搜索服务注入方式 | builder.py 创建服务实例，注入工具 | 与现有 drug_repo_factory / retriever 注入模式一致 |
| 来源标注方式 | Prompt 驱动分块 | 用户选择"分块标注"方案；LLM 根据工具来源标记自行组织回复结构 |
| 消息顺序修复策略 | react_node 内 normalize 后统一传递 | 避免 agent.py 和 react.py 各做各的转换导致不一致 |
| 状态一致性判定依据 | `phase` + `recommendations` 为主 | `consult_next_action` 跨 turn 可能过时；phase 和 recommendations 反映真实状态 |

## 不受影响的模块

- Dispatcher / Consult / Safety / Recommend / Inventory 节点
- LangGraph 图结构
- ToolRegistry 核心逻辑
- DrugRepository / DrugManualRetriever
- LLMClient / LLMProfile
- ConversationState 字段定义
