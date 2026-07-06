# Agent 架构 v2 Checklist

> 每一项通过运行代码或观察行为来验证，聚焦系统行为。

## 实现完整性

- [ ] **Dispatcher 输出执行计划**：输入纯症状描述 → 验证 `dispatcher_result.actions` 为 `[{action:"workflow",...}]`（运行 `test_dispatcher.py`）
- [ ] **Dispatcher 输出混合意图**：输入"咳嗽吃什么药，布洛芬有什么作用" → 验证 `dispatcher_result.actions` 长度为 2，workflow 排在 react 之前（运行 `test_dispatcher.py`）
- [ ] **LLMProfile 模型可用**：`python -c "from app.llm.profile import LLMProfile; p = LLMProfile(model='qwen-turbo'); assert p.model == 'qwen-turbo'"`
- [ ] **LLMClient 支持 Profile**：用 `profile=LLMProfile(model="qwen-turbo")` 调用 `generate()`，验证请求中的 model 为 qwen-turbo（运行 `test_react_agent.py` 中的 mock 断言）
- [ ] **LLMClient.generate_with_tools() 可用**：传入 tools 定义，验证返回的 response 包含 tool_calls 字段或 content 字段（运行 `test_react_agent.py`）
- [ ] **ReactAgent 模块可独立实例化**：不 import LangGraph 即可 `ReactAgent(llm_client, prompt, registry, profile)`（运行 `test_react_agent.py`）
- [ ] **ToolRegistry 注册 5 个工具**：`registry.get_definitions()` 返回 5 个 OpenAI 格式 tool 定义（运行 `test_orchestrator.py` 或独立验证脚本）
- [ ] **Orchestrator 节点存在**：`graph.get_graph().nodes` 包含 `"orchestrator"`（运行 `test_chat_flow.py::test_graph_compiles`）
- [ ] **explain 节点已删除**：`graph.get_graph().nodes` 不包含 `"explain"`（运行 `test_chat_flow.py::test_graph_compiles`）
- [ ] **router.py 已删除**：`grep -r "from app.graph.router import" app/` 无结果
- [ ] **图结构为线性**：`graph.get_graph().edges` 中 dispatcher → orchestrator → end 为无条件边（运行 `test_chat_flow.py`）
- [ ] **Settings 新增 4 个 LLMProfile 字段**：`Settings().llm_dispatcher` / `llm_consult` / `llm_react` / `llm_recommend` 均存在（运行 `python -c` 验证）

## 场景验收（对应 spec AC1-AC8）

- [ ] **AC1 — 纯症状描述**：用户输入"我头疼咳嗽两天了" → dispatcher 输出 `[{action:"workflow"}]` → orchestrator 执行 consult → 返回追问语或推荐
- [ ] **AC2 — 纯药品咨询**：用户输入"布洛芬有什么副作用" → dispatcher 输出 `[{action:"react", query:"布洛芬有什么副作用"}]` → react_agent 调用 search_drug + search_manual → 返回副作用说明
- [ ] **AC3 — 混合意图**：用户输入"咳嗽吃什么药，布洛芬有什么作用" → dispatcher 输出 `[{workflow},{react}]` → orchestrator 先执行 consult 再执行 react_agent → 最终回复包含追问/推荐 + 布洛芬说明，两段内容自然衔接
- [ ] **AC4 — workflow ask + react 衔接**：用户之前描述症状 → workflow 追问"请问咳嗽多久了？" → react 回复自然衔接到追问后面，不重复不突兀
- [ ] **AC5 — workflow done + react 衔接**：用户信息充分 → 推荐完成 → react 的回复利用推荐结果做更精准回答（如"布洛芬对干咳帮助不大，右美沙芬更对症"）
- [ ] **AC6 — "这个药"指代**：用户已获得推荐 → 问"这个药的副作用是什么" → react_agent 通过 `get_recommendation` 工具解析"这个药" → 正确回答
- [ ] **AC7 — 药品对比**：用户问"布洛芬和对乙酰氨基酚哪个好" → react_agent 分别查询两个药 → 对比回复
- [ ] **AC8 — 闲聊/放弃**：用户说"谢谢"或"算了去医院" → react_agent 不调用工具 → 友好回复/告别

## 回归验证

- [ ] **现有 157 测试全部通过**：`python -m pytest tests/ -v` 0 失败
- [ ] **consult 行为不变**：所有 test_consult_agent.py 测试通过
- [ ] **safety 行为不变**：所有 test_rules_engine.py 测试通过
- [ ] **recommend 行为不变**：所有 test_scoring_*.py 测试通过
- [ ] **symptom_normalizer 行为不变**：所有 test_symptom_normalizer.py 测试通过

## 健壮性

- [ ] **ReactAgent 工具调用失败不中断**：mock 工具抛异常 → agent 继续运行，最终返回部分结果或告知用户
- [ ] **ReactAgent 超限强制总结**：mock LLM 始终返回 tool_calls（超过 max_iterations）→ agent 强制总结，返回有效回复
- [ ] **LLM 不可用降级**：mock LLM 抛异常 → ReactAgent._format_raw_result() 把工具数据拼成可读回复
- [ ] **workflow 失败不阻止 react**：mock consult 抛异常 → 日志记录错误 → react 仍然正常执行
- [ ] **orchestrator 编排开销 <50ms**：纯逻辑路径（无 workflow 无 react 或简单拼接），计时验证

## 权限与安全

- [ ] **工具只读**：5 个 tool executor 不包含任何 INSERT/UPDATE/DELETE 操作（代码审查 + `grep` 确认）
- [ ] **react_agent 不修改 state**：agent 输出只有 final_response，不写 consult_slots 或 recommendations（代码审查）
- [ ] **Agent System Prompt 包含权限约束**：prompt 中明确写"你只能查询信息，不能修改任何数据"

## 可观测性

- [ ] **Dispatcher 输出完整 execution_plan**：`node_events` 中记录 `{node:"dispatcher", actions:[...]}`
- [ ] **ReactAgent 每步记录**：`node_events` 中记录每轮工具调用（tool_name + arguments + success/fail）
- [ ] **最终 response 不暴露工具调用细节**：用户看到的文本中不含 `search_drug` `tool_calls` 等内部术语

## 端到端场景

- [ ] **E2E-1**：用户"你好" → dispatcher 输出 `[react, chat]` → react 友好问候 → 回复不包含工具调用痕迹
- [ ] **E2E-2**：用户"头疼两天了" → dispatcher 输出 `[workflow]` → consult 追问 → 回复包含追问语（"有没有发烧""多大年龄"等）
- [ ] **E2E-3**：完整问诊流程 → consult 多轮 → done → safety PASS → recommend 返回 Top 3 → inventory 拼接库存 → 回复包含推荐药品 + 评分 + 免责声明
- [ ] **E2E-4**：用户获得推荐后说"这个药有什么副作用" → react_agent 通过 get_recommendation 解析 → search_manual 检索 → 正确回答
- [ ] **E2E-5**：用户"咳嗽吃啥药，另外布洛芬能退烧吗" → workflow（追问或推荐）+ react（布洛芬退烧说明）→ 两段回复自然衔接
- [ ] **E2E-6**：安全拦截场景 → 用户"发烧 39.5 三天了" → safety BLOCK → 返回就医警告，不进入推荐
