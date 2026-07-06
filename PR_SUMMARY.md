# PR Summary：ReactAgent v2.1 — 三级数据源策略 + 防编造护栏 + Bug 修复

**Branch**: `master`  
**Files**: 7 modified + 3 new (untracked) | **Diff**: `+246 / -32` (tracked) + new files  
**Key**: `app/search/` — 新增联网搜索模块；`search_web` 成为第 6 个工具

---

## 一、概述

本次变更是 ReactAgent 的 v2.1 完善升级，聚焦三个目标：

1. **三级数据源策略** — 本地 DB → 本地 Milvus RAG → Tavily 联网搜索，逐级兜底
2. **防编造护栏** — 空结果标记机制 + Prompt 约束，杜绝 LLM 凭记忆编造药品信息
3. **两个 Bug 修复** — F1（消息重复 normalize 导致顺序错乱）+ F2（workflow 上下文误判）

---

## 二、逐文件变更

### 1. `app/search/` — **新模块**：联网搜索服务（3 文件）

```
app/search/
├── __init__.py      # 导出 WebSearchService, TavilySearchService, WebSearchResult, WebSearchResponse
├── schemas.py       # WebSearchResult（title/snippet/url/source） + WebSearchResponse
└── service.py       # WebSearchService (ABC) + TavilySearchService (httpx 实现)
```

**设计**：`WebSearchService` 是抽象基类，`TavilySearchService` 是默认实现。未来可替换为 Bing/Google 等后端，只需实现 `search()` 和 `is_available`。

**Tavily 实现细节**：
- API: `https://api.tavily.com/search`
- `search_depth=basic` — 快速搜索模式
- 超时 + HTTP 异常全量兜底（不抛异常，返回 `WebSearchResponse` 含 `warning` 字段）
- API Key 为空时 `is_available=False`，SearchWebTool 直接返回不可用

### 2. `app/agent/react/tools/search_web.py` — **新文件**：SearchWebTool

```python
class SearchWebTool(BaseTool):
    capabilities = ["web_search"]
    fallback_tools = []        # 最后一级，无替代工具
```

- 能力标签 `web_search`（供 LLM 按场景选择）
- 输入 `query` + 可选 `num_results`
- 返回结构化 dict：`found`, `results[]`（每条含 `title/snippet/url/source="web"`）, `warning`
- `@property definition` 描述明确标注"仅在本地工具返回空或不充分时使用"

### 3. `app/config.py` (+14) — 联网搜索配置

```python
web_search_enabled: bool = True             # 全局开关
tavily_api_key: str = ""                    # Tavily API Key（.env: TAVILY_API_KEY=tvly-xxx）
web_search_timeout: float = 10.0            # 请求超时（秒）
web_search_max_results: int = 5             # 每次搜最多返回结果数
```

### 4. `app/agent/react/agent.py` (+68/-10) — 空结果包装 + 降级增强

**新增 `_wrap_tool_result()` 函数**（~35 行）：

处理三种情况：
| 工具返回 | 包装结果 | 语义 |
|---------|---------|------|
| 成功 + `[]` 或 `{}` | `{"found": false, "message": "本地知识库未找到..."}` | 空结果 → 触发 LLM 找替代工具 |
| 成功 + `{"empty": true}` | `{"found": false, "message": "..."}` | 显式空标记（如服务不可用） |
| 成功 + 非空数据 | 保持原样 + 补充 `found: true`（若无） | 正常数据 |

**`_handle_tool_calls()` 变更**：工具结果不再直接 `json.dumps(result.data)`，改为 `_wrap_tool_result(result)` 处理后再序列化。

**`_format_raw_result()` 增强**：降级回复的信息展示从 `item.get("name")` 改为 `item.get("generic_name") or item.get("name") or item.get("title")`，兼容三种数据格式。

### 5. `app/agent/prompts.py` (+43/-3) — Prompt 防编造 + 三级策略规则

**REACT_SYSTEM_PROMPT 增强**（~40 行新增）：

| 规则 | 内容 |
|------|------|
| 严禁编造 | 新增禁止使用"基于国家药监局公开信息""根据临床常规""据我所知"等措辞 |
| 空结果行为 | 4 步强制流程：不编造 → 换工具 → 联网搜索兜底 → 全部失败时标准拒绝话术 |
| 联网搜索规则 | 5 条：确认前置条件、query 格式、来源 URL、网络数据专区、免责声明 |
| 来源标注规则 | 本地数据正常回答；有网络数据则分「📋 本地知识库」「🌐 网络补充」两个区域 |
| 工具表更新 | 新增 `search_web` 行：本地工具均返回空或不充分时使用 |
| 拒绝话术 | 若没有能回答的信息则拒绝回答，建议咨询人工/专业医生 |

**CONSULT_PROMPT 微调**：response 风格要求从"不要太机械"扩展为"专业、清晰、自然，不要机械重复用户问题"。

### 6. `app/graph/nodes/react.py` (+44/-14) — 两个 Bug 修复

**F1 — 消息 normalize 修复**：
```python
# 旧（bug）：messages 被 react_agent._build_messages() 内部 normalize 一次，
#          react_node 自己又 normalize 一次 → 消息顺序可能错乱
normalized = normalize_messages(messages)

# 新（fix）：在 react_node 入口统一 normalize 一次，后面全部使用
raw_messages = state.get("messages", [])
messages = normalize_messages(raw_messages)
```

**F2 — workflow 上下文判定修复**：
```python
# 旧（bug）：只看 consult_next_action，忽略 phase 和 recommendations 状态
workflow_action = "done" if consult_next_action == "done" else "ask"

# 新（fix）：综合判定（phase + recommendations + consult_next_action）
if recommendations and current_phase in ("recommending", "ended"):
    workflow_action = "done"
elif consult_next_action == "ask" and current_phase == "consulting":
    workflow_action = "ask"
elif recommendations:
    workflow_action = "done"    # 有推荐但 phase 异常 → 按 done 兜底
```

**workflow_response 回填**：当 `response` 为空但有 recommendations 时，自动生成摘要：
```python
if not workflow_response and recommendations:
    drug_names = [r.get("generic_name", "") for r in recommendations[:3]]
    workflow_response = f"系统已推荐：{'、'.join(drug_names)}"
```

**phase 判定修复**：`workflow_action == "ask"` 且 `current_phase == "consulting"` 才设 `phase=consulting`，避免误判。

### 7. `app/graph/builder.py` (+4) — 第 6 个工具注册

```python
web_search_service = TavilySearchService(Settings())
tools: list[BaseTool] = [
    SearchDrugTool(...),
    GetDrugDetailTool(...),
    SearchManualTool(...),
    SearchWebTool(web_search_service=web_search_service),  # 新增
    GetRecommendationTool(...),
    GetUserProfileTool(...),
]
```

### 8. `app/agent/react/tools/__init__.py` (+2) — 导出更新

新增 `SearchWebTool` 到 `__all__`。

### 9. `tests/unit/test_react.py` (+94) — 回归测试

新增 2 个 Bug 修复回归测试 + 1 个 workflow 上下文测试：
- `test_react_node_workflow_done_with_empty_response` — F2：空 response 但有 recommendations 时 workflow_context 为 done
- `test_react_node_message_order` — F1：传入 react_agent 的消息是已 normalize 的 dict，role 统一

### 10. `tests/unit/test_search_web.py` — **新文件**（150 行）

- `TestSearchWebTool`：4 个用例（正常返回、空结果、source 标记、fallback_tools 检查）
- `TestEmptyResultWrapping`：6 个用例（空列表、空字典、非空保留、found 补全、error 透传、empty 标记保持）
- 1 个 Prompt 回归测试（search_web 在 REACT_SYSTEM_PROMPT 中）

---

## 三、核心架构决策

### 决策 1：三级数据源漏斗

```
用户问题
  → 第 1 级: search_drug / get_drug_detail（PostgreSQL）
     ↓ 空？
  → 第 2 级: search_manual（Milvus RAG 向量检索）
     ↓ 空？
  → 第 3 级: search_web（Tavily 联网搜索）
     ↓ 空？
  → 标准拒绝话术："建议咨询医生/药师"
```

三级之间通过 `_wrap_tool_result()` 的 `found: false` 标记串联——LLM 看到 `found=false` 后按 Prompt 规则自动尝试下一级。

### 决策 2：空结果标记机制

**问题**：工具返回 `[]` 时 LLM 可能理解为"没有找到"但也可能理解为"工具调用出错"或"不需要展示"。

**方案**：`_wrap_tool_result()` 在所有空结果上显式添加 `{"found": false, "message": "..."}` 标记，让 LLM 明确知道"此数据源无数据"，从而触发 Prompt 定义的备用工具流程。

### 决策 3：Tavily 而非直接调 Bing API

Tavily 专为 AI Agent 设计，返回的 content 字段是提取干净的文本（非 HTML），适合 LLM 直接消费。同时 `httpx` 异步调用 + 全异常兜底，不影响主流程稳定性。

### 决策 4：WebSearchService 抽象

```python
class WebSearchService(ABC):
    async def search(query, num_results) -> WebSearchResponse: ...
    def is_available(self) -> bool: ...
```

未来切换到 Bing/Gemini/SerpAPI 只需实现这个接口，替换 `TavilySearchService(Settings())` 即可。

---

## 四、Breaking Changes

### BC-1：工具数量从 5 变 6

`react_agent.tool_registry` 从 5 工具变为 6 工具。如果有代码硬编码了工具数量 `==5` 的断言，需要更新为 `>=6`。

### BC-2：`_handle_tool_calls()` 的工具结果格式变更

工具结果从直接序列化变为 `_wrap_tool_result()` 包装后序列化——LLM 看到的 tool message content 现在包含 `found`、`message`、`source` 等新增字段。

### BC-3：需要 Tavily API Key

生产环境需在 `.env` 配置 `TAVILY_API_KEY=tvly-xxx`。开发环境可设置 `web_search_enabled=false` 跳过。

---

## 五、业务影响

| 维度 | 影响 |
|------|------|
| **防编造能力** | 三级门禁（空标记 + Prompt 禁止 + 拒绝话术），LLM 无法用"据我所知"绕过工具查询 |
| **数据覆盖** | 本地知识库（DB + Milvus）覆盖不到的药品信息，自动联网搜索兜底 |
| **用户透明度** | 网络数据明确标注来源 URL + 免责声明，用户可区分本地和网络来源 |
| **消息处理** | F1 修复后 normalize 只做一次，消息顺序正确，不再出现 system 消息被当 user 的 bug |
| **workflow 衔接** | F2 修复后 ReactAgent 正确感知 workflow 完成状态，推荐后的追问能准确引用推荐结果 |
| **开发环境** | `web_search_enabled=false` 可完全关闭联网搜索，不依赖外部 API |
| **扩展性** | WebSearchService ABC 设计支持任意搜索后端替换 |

---
