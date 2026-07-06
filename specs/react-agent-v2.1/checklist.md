# ReactAgent v2.1 Checklist

> 每一项通过运行代码或观察行为来验证，聚焦系统行为。

## 实现完整性

- [ ] `app/search/` 模块存在且可导入（验证：`python -c "from app.search import WebSearchService, BingSearchService, WebSearchResult"`）
- [ ] `SearchWebTool` 已注册到 ReactAgent（验证：`python -c "from app.graph.builder import build_graph; print('OK')"`）
- [ ] `web_search_*` 配置字段存在于 Settings（验证：`python -c "from app.config import Settings; print(Settings().web_search_enabled)"`）
- [ ] REACT_SYSTEM_PROMPT 包含 search_web 工具说明（验证：`python -c "from app.agent.prompts import REACT_SYSTEM_PROMPT; assert 'search_web' in REACT_SYSTEM_PROMPT"`）
- [ ] REACT_SYSTEM_PROMPT 包含空结果反编造约束（验证：`python -c "from app.agent.prompts import REACT_SYSTEM_PROMPT; assert 'found' in REACT_SYSTEM_PROMPT.lower() or '空结果' in REACT_SYSTEM_PROMPT"`）

## Bug 修复验证

- [ ] F1: react_node 取 query 不依赖消息遍历顺序（验证：`python -m pytest tests/unit/test_react.py -v -k "message_order or react_node"`）
- [ ] F2: workflow done + recommendations 时 workflow_context.workflow_action = "done"（验证：`python -m pytest tests/unit/test_react.py -v -k "workflow_done"`）
- [ ] F2: response 为空但 recommendations 有值时，react_node 能构造有效的 workflow_response（验证：测试 `test_react_node_workflow_done_with_empty_response` 通过）

## 反编造验证

- [ ] 工具返回空列表 `[]` 时，LLM 收到的是 `{"found": false}` 而不是裸 `[]`（验证：`python -m pytest tests/unit/test_search_web.py -v -k "empty_list_wrapped"`）
- [ ] 正常工具结果不被错误包装（验证：`python -m pytest tests/unit/test_search_web.py -v -k "non_empty"`）
- [ ] 所有工具返回空时，AI 回复不含编造的药品信息（验证：`python -m pytest tests/unit/test_react_agent.py -v -k "empty_tool"`）
- [ ] LLM 不可用时降级回复不输出药品信息碎片（验证：检查 `_format_raw_result` 返回的文本不含具体药品功效/副作用/禁忌描述）

## 联网搜索验证

- [ ] SearchWebTool 在 tools 列表中正确注册，LLM 可调用（验证：`python -m pytest tests/unit/test_search_web.py -v -k "search_web_returns"`）
- [ ] 联网搜索不可用时返回明确错误信息（验证：`python -m pytest tests/unit/test_search_web.py -v -k "unavailable"`）
- [ ] 网络搜索结果包含 source URL（验证：检查 `WebSearchResponse.results[0].url` 非空）
- [ ] 网络搜索结果带免责声明 warning 字段（验证：检查 `WebSearchResponse.warning` 非空）

## 来源标注验证

- [ ] REACT_SYSTEM_PROMPT 要求网络数据标注 `🌐 网络补充` 区域（验证：prompt 中包含来源标注格式说明）
- [ ] REACT_SYSTEM_PROMPT 要求网络数据附带来源 URL（验证：prompt 中包含 "来源" 或 "source" 关键词）

## 编译与测试

- [ ] 项目无导入错误（验证：`python -c "import app"` 无报错）
- [ ] 所有单元测试通过（验证：`python -m pytest tests/ -v`，预期 194+ 个测试全部通过）
- [ ] 新增测试不少于 6 个（验证：`python -m pytest tests/ --collect-only | grep -c "test_"`）

## 端到端场景

- [ ] 场景 1：用户问"布洛芬有什么副作用"，本地 Milvus 有数据 → AI 基于说明书原文回答，不触发联网搜索
- [ ] 场景 2：用户问"氢溴酸右美沙芬孕妇能用吗"，Milvus 返回空 → AI 触发 search_web → 回复中 `🌐 网络补充` 区域附带来源链接
- [ ] 场景 3：用户问一种冷门药，所有工具（DB + Milvus + Web）均无数据 → AI 如实告知无数据，建议咨询药师，不编造
- [ ] 场景 4：workflow 推荐完成后用户问"这些药哪个孕妇不能用" → react_node 正确读取 recommendations，state 一致
