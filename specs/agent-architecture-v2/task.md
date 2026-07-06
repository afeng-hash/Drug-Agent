# Agent 架构 v2 Tasks

## 文件清单

| 操作 | 文件 | 职责 |
|------|------|------|
| **新建** | `app/llm/profile.py` | LLMProfile 模型定义 |
| **修改** | `app/llm/client.py` | 支持 Profile 参数，新增 `generate_with_tools()` |
| **修改** | `app/config.py` | 新增 4 个 LLMProfile 字段 |
| **新建** | `app/agent/react/__init__.py` | 模块导出 |
| **新建** | `app/agent/react/schemas.py` | ToolDefinition, ToolCall, ToolResult, AgentStep, AgentResult, WorkingMemory |
| **新建** | `app/agent/react/tools.py` | ToolRegistry 类 + 5 个工具 executor |
| **新建** | `app/agent/react/memory.py` | WorkingMemory 管理 |
| **新建** | `app/agent/react/agent.py` | ReactAgent 类 — 工具调用循环 |
| **修改** | `app/agent/prompts.py` | DISPATCHER_PROMPT 重写为执行计划格式 |
| **修改** | `app/graph/nodes/dispatcher.py` | DispatcherDecision 改为 actions[] |
| **修改** | `app/graph/state.py` | 文档更新，phase 可选取值更新 |
| **新建** | `app/graph/nodes/orchestrator.py` | 编排节点 |
| **修改** | `app/graph/builder.py` | 图结构简化为线性管道 |
| **修改** | `app/graph/nodes/consult.py` | 函数保留，不再作为独立图节点 |
| **修改** | `app/graph/nodes/safety_check.py` | 函数保留（可直接调用） |
| **修改** | `app/graph/nodes/recommend.py` | 函数保留（可直接调用） |
| **修改** | `app/graph/nodes/inventory.py` | 函数保留（可直接调用） |
| **删除** | `app/graph/nodes/explain.py` | 功能被 ReactAgent 吸收 |
| **删除** | `app/graph/router.py` | 图变成线性，不需要条件边路由 |
| **新建** | `tests/unit/test_react_agent.py` | ReactAgent 单元测试 |
| **新建** | `tests/unit/test_orchestrator.py` | Orchestrator 单元测试 |
| **修改** | `tests/unit/test_dispatcher.py` | 适配 actions[] 输出格式 |
| **修改** | `tests/integration/test_chat_flow.py` | 适配新图结构 |
| **修改** | `tests/conftest.py` | fixture 适配 |

## T1: LLMProfile 模型

**文件**: `app/llm/profile.py`（新建）
**依赖**: 无
**步骤**:
1. 定义 `LLMProfile` Pydantic 模型，字段：model(str, default="qwen-plus"), temperature(float, 0.3), max_tokens(int, 1024), timeout(float, 30.0)
2. 添加 `model_dump()` 兼容方法（Pydantic v2 自带）

**验证**: `python -c "from app.llm.profile import LLMProfile; p = LLMProfile(); print(p.model)"`

---

## T2: LLMClient 支持多 Profile + generate_with_tools

**文件**: `app/llm/client.py`（修改）
**依赖**: T1
**步骤**:
1. `__init__` 中新增 `self.default_profile = LLMProfile(model=settings.llm_model)`
2. `generate()` 和 `generate_structured()` 方法签名新增可选参数 `profile: LLMProfile = None`
3. 方法内部用 `p = profile or self.default_profile` 取 Profile，替换原来硬编码的 `self.model`/`self.settings.llm_model`
4. 新增 `generate_with_tools(messages, tools, profile=None)` → 调用 `chat.completions.create` 并传入 `tools` 参数，返回原始 response 对象（不循环，循环在 ReactAgent 中）
5. `stream()` 和 `embed()` 保持向后兼容——接受可选的 `profile` 参数

**验证**: `python -c "from app.llm.client import LLMClient; c = LLMClient(settings); print(c.default_profile)"`

---

## T3: Settings 新增 LLMProfile 字段

**文件**: `app/config.py`（修改）
**依赖**: T1
**步骤**:
1. 新增 4 个 dict 字段：
   - `llm_dispatcher: dict = {"model": "qwen-turbo", "temperature": 0.1, "max_tokens": 256}`
   - `llm_consult: dict = {"model": "qwen-plus", "temperature": 0.3, "max_tokens": 1024}`
   - `llm_react: dict = {"model": "qwen-plus", "temperature": 0.3, "max_tokens": 1024}`
   - `llm_recommend: dict = {"model": "qwen-plus", "temperature": 0.3, "max_tokens": 512}`
2. 新增辅助方法 `get_profile(field_name: str) -> LLMProfile`：把 dict 转成 LLMProfile 对象
3. 保留 `llm_model` 字段作为默认（向后兼容）

**验证**: `python -c "from app.config import Settings; s = Settings(); print(s.get_profile('llm_dispatcher'))"`

---

## T4: ReactAgent 数据模型

**文件**: `app/agent/react/schemas.py`（新建）
**依赖**: 无
**步骤**:
1. 定义 `ToolDefinition`：name, description, parameters(dict), capability(str, default="read")
2. 定义 `ToolCall`：id(str), tool_name(str), arguments(dict)
3. 定义 `ToolResult`：tool_name, success(bool), data(Any), error(Optional[str])
4. 定义 `AgentStep`：iteration(int), tool_calls(list), tool_results(list)
5. 定义 `AgentResult`：final_response(str), steps(list), total_iterations(int), total_time_ms(float)
6. 定义 `WorkingMemory`：intermediate_findings(dict), context_notes(list)

**验证**: `python -c "from app.agent.react.schemas import ToolDefinition; t = ToolDefinition(name='test', description='test', parameters={}); print(t.name)"`

---

## T5: ToolRegistry

**文件**: `app/agent/react/tools.py`（新建）
**依赖**: T4
**步骤**:
1. 定义 `ToolRegistry` 类：
   - `_definitions: dict[str, ToolDefinition]`
   - `_executors: dict[str, Callable]`
   - `register(definition: ToolDefinition, executor: Callable)` — 注册工具
   - `get_definitions() -> list[dict]` — 返回 OpenAI tool 格式列表
   - `get_executor(name: str) -> Callable` — 获取执行函数
   - `async execute(tool_name: str, arguments: dict) -> ToolResult` — 执行工具并捕获异常
2. 工具执行时捕获所有异常，返回 `ToolResult(success=False, error=str(e))`

**验证**: 单元测试——注册 mock 工具，调用 execute，验证 ToolResult 结构

---

## T6: WorkingMemory

**文件**: `app/agent/react/memory.py`（新建）
**依赖**: T4
**步骤**:
1. 定义 `WorkingMemory` 类：
   - `findings: dict[str, Any]` — 工具名 → 摘要结果
   - `add_finding(tool_name: str, data: Any)` — 记录工具结果
   - `get_finding(tool_name: str) -> Any` — 读取结果
   - `clear()` — 重置
2. 用于在 ReactAgent 循环中缓存工具结果，避免重复调用

**验证**: 单元测试——add_finding 后 get_finding 返回正确数据

---

## T7: ReactAgent 核心类

**文件**: `app/agent/react/agent.py`（新建）
**依赖**: T2, T4, T5, T6
**步骤**:
1. 定义 `ReactAgent` 类：
   ```python
   class ReactAgent:
       def __init__(self, llm_client, system_prompt, tool_registry, profile, max_iterations=5)
       async def run(self, user_message, history, context=None) -> AgentResult
   ```
2. `run()` 实现 ReAct 循环：
   a. 构建初始 messages：system_prompt + history[-10:] + user_message
   b. 如果有 context，动态注入上下文段落到 system_prompt 末尾
   c. 循环 iteration in range(max_iterations):
      - 调用 `llm_client.generate_with_tools(messages, tools)`
      - 解析 response：如果有 tool_calls → 并行执行 → 追加到 messages → continue
      - 如果是纯文本 → `AgentResult(final_response=content, ...)`
   d. 超过 max_iterations → 调用 `_force_summarize()` 生成最终回复
3. `_force_summarize()` 方法：追加一条 system 消息"请基于以上信息给出最终回复"，再做一次不带 tool 的 LLM 调用
4. `_format_raw_result()` 方法：LLM 完全不可用时，把工具原始数据拼成降级回复
5. 记录每步到 `self.steps`，计算耗时

**验证**: `python -m pytest tests/unit/test_react_agent.py -v` —— 覆盖闲聊、单工具调用、多工具调用、超限强制总结、LLM 异常降级

---

## T8: ReactAgent 模块导出

**文件**: `app/agent/react/__init__.py`（新建）
**依赖**: T4, T5, T6, T7
**步骤**:
1. 导出: `ReactAgent`, `ToolDefinition`, `ToolRegistry`, `AgentResult`, `AgentStep`, `WorkingMemory`

**验证**: `python -c "from app.agent.react import ReactAgent, ToolDefinition, ToolRegistry, AgentResult"`

---

## T9: DISPATCHER_PROMPT 重写

**文件**: `app/agent/prompts.py`（修改）
**依赖**: 无
**步骤**:
1. 重写 DISPATCHER_PROMPT：
   - 角色：对话意图解析器
   - 输出格式：`{actions: [{action, intent, query, priority}]}`
   - 编排规则：workflow 优先于 react，最多 2 个 action
   - 纯症状描述 → [workflow]
   - 纯药品咨询/闲聊 → [react]
   - 混合意图 → [workflow, react]
   - 增加 3 个混合意图示例
2. 保留 CONSULT_PROMPT 不变

**验证**: 检查输出格式 JSON schema 是否与 DispatcherDecision.actions[] 一致

---

## T10: DispatcherDecision 改为执行计划

**文件**: `app/graph/nodes/dispatcher.py`（修改）
**依赖**: T9
**步骤**:
1. 新增 `ActionItem` Pydantic 模型：action, intent, query, priority
2. `DispatcherDecision` 改为 `actions: list[ActionItem]`
3. `dispatcher_node()` 更新：
   - 移除 previous_phase 处理逻辑（不再需要）
   - `dispatcher_result` 输出改为 `{"actions": decision.actions}`
   - 更新 docstring
4. `_fallback_route()` 改为 `_fallback_plan()`：返回 `{"actions": [ActionItem(action="react", intent="fallback", priority=1)]}`

**验证**: `python -m pytest tests/unit/test_dispatcher.py -v` —— 全部通过

---

## T11: State 文档更新

**文件**: `app/graph/state.py`（修改）
**依赖**: T10
**步骤**:
1. 更新 `dispatcher_result` 文档注释为 actions[] 结构
2. 更新 `phase` 文档：移除 "explaining" 可选取值，新增 "reacting"
3. 其他字段文档不变

**验证**: `python -c "from app.graph.state import ConversationState; print('ok')"`

---

## T12: Orchestrator 编排节点

**文件**: `app/graph/nodes/orchestrator.py`（新建）
**依赖**: T7, T8, T10, T11（依赖 ReactAgent + 现有 node 函数）
**步骤**:
1. 定义 `orchestrator_node(state, llm_client, ...)` 函数
2. 实现 3 步流程：
   **Step 1 — Workflow**:
   a. 读取 actions[] 中 action="workflow" 的项
   b. 调用 `run_consult(messages, slots, intent, ...)`
   c. 如果 `next_action == "done"`：调用 `rule_engine.check()` → PASS 则调用 `recommend_node()` → `inventory_node()`
   d. 如果 `next_action == "ask"`：跳过 safety/recommend/inventory
   **Step 2 — React**:
   a. 读取 actions[] 中 action="react" 的项
   b. 构建 react_context（如果 workflow 存在）
   c. 调用 `react_agent.run(query, history, context)`
   **Step 3 — Assemble**:
   a. 拼接 workflow_response + react_response
3. 返回 `{response, phase, consult_slots, consult_rounds, ...}`
4. 在关键决策点加注释标注"路由点"

**验证**: `python -m pytest tests/unit/test_orchestrator.py -v`

---

## T13: 图结构简化

**文件**: `app/graph/builder.py`（修改）
**依赖**: T12
**步骤**:
1. 移除 `explain`、`consult`、`safety_block`、`recommend`、`inventory` 的 `graph.add_node()` 调用
2. 移除所有 `graph.add_conditional_edges()` 调用
3. 新增 `graph.add_node("orchestrator", _make_orchestrator(...))`
4. 简单线性边：`intake → dispatcher → orchestrator → end → END`
5. `_make_orchestrator()` 工厂函数注入所有依赖（LLMClient, RuleEngine, ReactAgent, DB factories 等）
6. 保留 `_make_end()` 不变

**验证**: `python -c "from app.graph.builder import build_graph; graph = build_graph(...); nodes = graph.get_graph().nodes; assert 'orchestrator' in nodes; assert 'explain' not in nodes"`

---

## T14: 删除 explain.py 和 router.py

**文件**: 删除 `app/graph/nodes/explain.py`，`app/graph/router.py`
**依赖**: T12, T13
**步骤**:
1. 删除 `app/graph/nodes/explain.py`
2. 删除 `app/graph/router.py`
3. 全局搜索 `from app.graph.nodes.explain import` 和 `from app.graph.router import`，确认无残留引用

**验证**: `grep -r "explain_node\|route_after_dispatcher\|route_after_consult\|route_after_safety\|from.*router import" app/` 无结果

---

## T15: 5 个工具 executor 注册

**文件**: 在 orchestrator 工厂中注册（`app/graph/builder.py` 的 `_make_orchestrator` 内）
**依赖**: T5, T7, T13
**步骤**:
1. 创建 `ToolRegistry` 实例
2. 注册 5 个工具：
   ```python
   registry.register(
       ToolDefinition(name="search_drug", description="搜索药品...", parameters={...}),
       lambda query, limit=5: drug_repo.search(query, limit)
   )
   # ... 其他 4 个工具
   ```
3. 创建 `ReactAgent(llm_client, REACT_SYSTEM_PROMPT, registry, profile, max_iterations=5)`
4. 注入到 orchestrator_node 调用中

**验证**: `python -c "registry = ToolRegistry(); registry.register(...); assert len(registry.get_definitions()) == 5"`

---

## T16: 单元测试 — test_react_agent.py

**文件**: `tests/unit/test_react_agent.py`（新建）
**依赖**: T7, T8
**步骤**:
1. Mock LLMClient + 2 个 mock 工具
2. 覆盖场景：
   - 简单闲聊（无工具调用）→ 正确返回 final_response
   - 单工具调用（search_drug）→ 工具被调用，参数正确
   - 多工具调用（search_drug × 2）→ 两次工具都被正确调用
   - 多轮工具调用 → 第 1 轮结果影响第 2 轮
   - 超过 max_iterations → 强制总结
   - 工具执行异常 → 循环继续，不中断
   - LLM 异常 → _format_raw_result 降级

**验证**: `python -m pytest tests/unit/test_react_agent.py -v` —— 全部通过

---

## T17: 单元测试 — test_orchestrator.py

**文件**: `tests/unit/test_orchestrator.py`（新建）
**依赖**: T12
**步骤**:
1. Mock run_consult, rule_engine, recommend_node, inventory_node, react_agent
2. 覆盖场景：
   - 纯 workflow（done）→ 推荐链路被调用
   - 纯 workflow（ask）→ safety/recommend 不被调用
   - 纯 react → react_agent.run 被调用
   - workflow + react → 两者都被调用，输出正确拼接
   - safety BLOCK → recommend 不被调用，返回就医警告
   - workflow ask + react → 两者输出正确拼接

**验证**: `python -m pytest tests/unit/test_orchestrator.py -v` —— 全部通过

---

## T18: 适配现有测试

**文件**: `tests/unit/test_dispatcher.py`, `tests/integration/test_chat_flow.py`, `tests/conftest.py`
**依赖**: T10, T13
**步骤**:
1. `test_dispatcher.py`：更新 DispatcherDecision 的 mock 返回值为 actions[] 格式，更新 assert
2. `test_chat_flow.py`：更新 `test_graph_compiles` 中新图节点的 assert（orchestrator 替换 explain，explain 不应存在）
3. `conftest.py`：检查 dispatcher_result fixture 是否需要适配

**验证**: `python -m pytest tests/unit/test_dispatcher.py tests/integration/test_chat_flow.py -v`

---

## T19: 全量回归测试

**文件**: 无
**依赖**: T1-T18
**步骤**:
1. `python -m pytest tests/ -v`
2. 确认 157+ 测试通过
3. 修复任何失败

**验证**: 全部测试通过

---

## 执行顺序

```
T1 ──→ T2 ──→ T3
              │
T4 ──→ T5 ──→ T6 ──→ T7 ──→ T8
                              │
T9 ──→ T10 ──→ T11            │
        │                     │
        └─────────────────────┤
                              ▼
              T12 ──→ T13 ──→ T14 ──→ T15
               │
               └──→ T16 (可并行)
               └──→ T17 (可并行)
                     │
                     ▼
                    T18 ──→ T19
```
