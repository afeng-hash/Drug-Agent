# ReactAgent v2.1 Tasks

## 文件清单

| 操作 | 文件 | 职责 |
|------|------|------|
| 新建 | `app/search/__init__.py` | 模块导出 |
| 新建 | `app/search/schemas.py` | WebSearchResult, WebSearchResponse |
| 新建 | `app/search/service.py` | WebSearchService 抽象 + BingSearchService |
| 新建 | `app/agent/react/tools/search_web.py` | SearchWebTool |
| 新建 | `tests/unit/test_search_web.py` | 联网搜索 + 空结果行为测试 |
| 修改 | `app/config.py` | 增加 web_search_* 配置 |
| 修改 | `app/agent/prompts.py` | REACT_SYSTEM_PROMPT 增强 |
| 修改 | `app/agent/react/agent.py` | _handle_tool_calls 空结果包装 |
| 修改 | `app/graph/nodes/react.py` | F1 消息顺序 + F2 状态一致性 |
| 修改 | `app/graph/builder.py` | 注册 SearchWebTool |
| 修改 | `tests/unit/test_react.py` | 状态一致性测试 |
| 修改 | `tests/unit/test_react_agent.py` | 空结果行为测试 |

---

## T1: Config 扩展 — 联网搜索配置

**文件：** `app/config.py`
**依赖：** 无
**步骤：**
1. 在 Settings 类中新增 5 个字段：
   - `web_search_enabled: bool = True`
   - `web_search_api_key: str = ""`
   - `web_search_endpoint: str = "https://api.bing.microsoft.com/v7.0/search"`
   - `web_search_timeout: float = 10.0`
   - `web_search_max_results: int = 5`

**验证：** `python -c "from app.config import Settings; s = Settings(); print(s.web_search_enabled)"` 输出 `True`

---

## T2: 网络搜索模块 — schemas

**文件：** `app/search/__init__.py`, `app/search/schemas.py`
**依赖：** 无
**步骤：**
1. 创建 `app/search/` 目录
2. `schemas.py`：定义 `WebSearchResult` 和 `WebSearchResponse` Pydantic 模型
   - `WebSearchResult`: title, snippet, url, source="web"
   - `WebSearchResponse`: query, results (list[WebSearchResult]), total_estimated, source="web", warning
3. `__init__.py`：导出所有公开符号

**验证：** `python -c "from app.search import WebSearchResult, WebSearchResponse; print('OK')"`

---

## T3: 网络搜索模块 — BingSearchService

**文件：** `app/search/service.py`
**依赖：** T1, T2
**步骤：**
1. 定义 `WebSearchService` 抽象类（ABC），包含 `search()` 和 `is_available` property
2. 实现 `BingSearchService(WebSearchService)`：
   - `__init__` 接收 `Settings`，读取 api_key、endpoint、timeout、max_results
   - `is_available`：api_key 非空且 enabled
   - `search(query, num_results)`：调用 Bing API，解析 JSON 响应，返回 `WebSearchResponse`
   - 异常处理：网络错误 → 返回空的 WebSearchResponse（results=[]）
3. `__init__.py` 导出 `WebSearchService`, `BingSearchService`

**验证：** `python -c "from app.search import BingSearchService; print('OK')"`

---

## T4: SearchWebTool — 联网搜索工具类

**文件：** `app/agent/react/tools/search_web.py`
**依赖：** T2, T3
**步骤：**
1. 实现 `SearchWebTool(BaseTool)`：
   - `fallback_tools = []`（最后一级，无替代）
   - `capabilities = ["web_search"]`
   - `definition`：description 明确写明"仅在本地工具返回空或不充分时使用"，返回结果含来源 URL
   - `execute(query, num_results=5)`：调用 `WebSearchService.search()`，格式化为统一结构
2. 在 `tools/__init__.py` 中导出 `SearchWebTool`

**验证：** `python -c "from app.agent.react.tools import SearchWebTool; print('OK')"`

---

## T5: REACT_SYSTEM_PROMPT 增强

**文件：** `app/agent/prompts.py`
**依赖：** 无
**步骤：**
1. 在「工具选择指南」表格中新增一行：
   - "本地工具均无结果时" → **search_web** → 联网搜索兜底
2. 新增「联网搜索使用规则」小节：
   - 仅在所有本地工具（search_manual、get_drug_detail）返回空或不充分后才调用
   - 搜索 query 应包含药品名 + 用户问题的关键词
   - 结果含来源 URL，必须在回复中对网络数据标注来源
3. 新增「空结果行为（强制）」小节：
   - 工具返回 `{"found": false}` 时，说明本地无数据
   - 此时**绝对禁止**用训练数据补充药品信息
   - 应：尝试联网搜索 → 若无 → 如实告知用户无数据
4. 更新「回复要求」：
   - 新增「来源标注规则」：本地数据不标注区域；网络数据必须标注 `🌐 网络补充` 区域 + 来源链接 + 免责声明

**验证：** `python -c "from app.agent.prompts import REACT_SYSTEM_PROMPT; assert 'search_web' in REACT_SYSTEM_PROMPT"`

---

## T6: ReactAgent — 空结果代码级包装

**文件：** `app/agent/react/agent.py`
**依赖：** 无
**步骤：**
1. 修改 `_handle_tool_calls`：工具结果注入到 messages 前，检查 data 是否为空：
   - 空列表 `[]` → 包装为 `{"found": false, "results": [], "message": "本地知识库未找到相关信息"}`
   - 空 dict `{}` → 包装为 `{"found": false, "message": "未找到相关信息"}`
   - 带 error 的 dict → 保持原样
   - 非空数据 → 保持原样
2. 修改 `_format_raw_result`：LLM 完全不可用时的降级文案：
   - 去掉 `_FALLBACK_TEMPLATE` 中的 `{findings}` 模板（避免输出不完整数据）
   - 改为纯服务不可用提示

**验证：** 运行 `python -m pytest tests/unit/test_react_agent.py -v`，现有测试保持通过

---

## T7: react_node — F1 消息顺序修复

**文件：** `app/graph/nodes/react.py`
**依赖：** 无
**步骤：**
1. 在 react_node 开头统一 normalize messages：
   ```python
   raw_messages = state.get("messages", [])
   messages = normalize_messages(raw_messages)
   ```
2. query 提取逻辑优化：
   - 优先从 `dispatcher_result.actions` 中取 react action 的 query
   - fallback（actions 为空）：从 `messages`（已 normalize）的末尾取最后一条 user 消息
   - 去掉 `reversed()` + `normalize_messages()` 的重复调用
3. `react_agent.run()` 传入 `history=messages`（已 normalize 的版本），而非 raw_messages

**验证：** `python -m pytest tests/unit/test_react.py -v -k "react_node"`，所有 react_node 测试通过

---

## T8: react_node — F2 状态一致性修复

**文件：** `app/graph/nodes/react.py`
**依赖：** T7（同一文件，建议一起改）
**步骤：**
1. 重写 workflow_context 构建逻辑：
   ```python
   # 判断 workflow 真实完成状态
   phase = state.get("phase", "")
   if recommendations and phase in ("recommending", "ended"):
       workflow_action = "done"
   elif consult_next_action == "ask":
       workflow_action = "ask"
   else:
       workflow_action = "done"  # 有 recommendations 默认 done
   
   # response 为空但 recommendations 有值时，从 recommendations 构造摘要
   workflow_response = state.get("response", "")
   if not workflow_response and recommendations:
       drug_names = [r.get("generic_name", "") for r in recommendations[:3]]
       workflow_response = f"系统已推荐：{', '.join(drug_names)}"
   ```
2. 更新 `workflow_context` 的构建条件：只要有 recommendations 就传递上下文

**验证：** `python -m pytest tests/unit/test_react.py -v -k "react_node"`，所有测试通过

---

## T9: builder.py — 注册 SearchWebTool

**文件：** `app/graph/builder.py`
**依赖：** T3, T4
**步骤：**
1. 导入 `BingSearchService`、`SearchWebTool`、`Settings`
2. 在 `_make_react` 中创建 `BingSearchService(settings)` 实例
3. 在 tools 列表末尾增加 `SearchWebTool(web_search_service=web_search_service)`
4. `build_graph` 签名不变（web_search 通过 Settings 获取配置，不需要额外参数）

**验证：** `python -c "from app.graph.builder import build_graph; print('OK')"`

---

## T10: 单元测试 — 联网搜索

**文件：** `tests/unit/test_search_web.py`（新建）
**依赖：** T2, T3, T4
**步骤：**
1. `TestSearchWebTool`：
   - `test_search_web_returns_results`：mock WebSearchService，验证结果格式化正确
   - `test_search_web_empty_results`：mock 返回空，验证 LLM 收到 `{"found": false}` 标记
   - `test_search_web_service_unavailable`：mock is_available=False，验证报错信息
   - `test_search_web_lowest_priority`：验证 fallback_tools = []（最后一级）
2. `TestEmptyResultWrapping`：
   - `test_empty_list_wrapped_as_not_found`：工具返回 [] → 包装为 `{"found": false}`
   - `test_non_empty_list_preserved`：正常数据不被包装
   - `test_error_result_preserved`：error 结果不被额外包装

**验证：** `python -m pytest tests/unit/test_search_web.py -v`，全部通过

---

## T11: 单元测试 — 补充现有测试

**文件：** `tests/unit/test_react.py`、`tests/unit/test_react_agent.py`
**依赖：** T7, T8, T6
**步骤：**
1. `test_react.py` 新增：
   - `test_react_node_workflow_done_with_empty_response`：response="" 但有 recommendations → workflow_context 正确为 done
   - `test_react_node_message_order`：verify normalized messages passed to react_agent in correct order
2. `test_react_agent.py` 新增：
   - `test_empty_tool_results_not_fabricated`：所有工具返回空 → AI 回复不含编造的药品信息

**验证：** `python -m pytest tests/unit/test_react.py tests/unit/test_react_agent.py -v`，全部通过

---

## T12: 全量回归测试

**依赖：** T1-T11 全部完成
**步骤：**
1. `python -m pytest tests/ -v`
2. 确保 188 个原有测试全部通过
3. 确保新增测试全部通过

**验证：** 全部测试通过，预期新增 6-8 个测试

---

## 执行顺序

```
T1 → T2 → T3 → T4 → T9
                    ↘
T5（可并行）──────────→ T6（可并行）──┐
                                      ├→ T12
T7 → T8 ──────────────────────────────┤
                                      │
                        T10 ──→ T11 ──┘
```
