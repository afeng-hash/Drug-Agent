# Agent 架构 v2 Plan

## 架构概览

本次重构将当前"多节点 + 条件边"的 Graph 简化为"3 节点线性管道"，核心变化：

```
改前 (8 节点, 4 条条件边):
  intake → dispatcher ──→ consult ──→ safety_block ──→ recommend ──→ inventory ──→ end
                   │    │         │            │
                   │    │  (ask)→ end        (BLOCK)→ end
                   │    │
                   │    ├── explain ──→ end
                   │    └── end

改后 (4 节点, 0 条条件边):
  intake → dispatcher → orchestrator → end
```

**为什么用单节点编排而不是多节点条件边？**

因为 workflow 和 react 是**顺序执行、紧密耦合**的——react 需要 workflow 的输出来做衔接。如果拆成多个图节点，要么状态传递复杂，要么衔接不自然。放在一个 orchestrator 里，顺序调用、结果传递，逻辑内聚。

## 核心数据结构

### ExecutionPlan（替代 DispatcherDecision）

```python
# app/graph/nodes/dispatcher.py

class ActionItem(BaseModel):
    """执行计划中的一个动作。"""
    action: str          # "workflow" | "react"
    intent: str          # 意图分类
    query: str = ""      # react 动作时的用户问题（workflow 时可为空）
    priority: int = 1    # 执行顺序，1=先执行


class DispatcherDecision(BaseModel):
    """LLM 输出的执行计划。"""
    actions: list[ActionItem]  # 长度 1-2，按 priority 排序
```

### LLMProfile（多模型配置）

```python
# app/llm/profile.py

class LLMProfile(BaseModel):
    """单个 LLM 场景的配置。"""
    model: str = "qwen-plus"
    temperature: float = 0.3
    max_tokens: int = 1024
    timeout: float = 30.0
```

### ReactAgent 数据模型

```python
# app/agent/react/schemas.py

class ToolDefinition(BaseModel):
    """工具定义，同时用于 OpenAI function calling 和内部注册。"""
    name: str
    description: str
    parameters: dict          # JSON Schema for arguments
    capability: str = "read"  # 权限级别

class ToolCall(BaseModel):
    """LLM 发起的一次工具调用。"""
    id: str
    tool_name: str
    arguments: dict

class ToolResult(BaseModel):
    """单次工具调用的结果。"""
    tool_name: str
    success: bool
    data: Any = None
    error: str | None = None

class AgentStep(BaseModel):
    """Agent 循环中的一步。"""
    iteration: int
    thought: str | None
    tool_calls: list[ToolCall]
    tool_results: list[ToolResult]

class AgentResult(BaseModel):
    """Agent 运行完成的输出。"""
    final_response: str
    steps: list[AgentStep]
    total_iterations: int
    total_time_ms: float

class WorkingMemory(BaseModel):
    """Agent 工作记忆（单次调用内）。"""
    intermediate_findings: dict = {}  # tool_name → summarized result
    context_notes: list[str] = []     # agent 自己的备注
```

## 模块设计

### 模块 A: Dispatcher（修改）

**文件**: `app/graph/nodes/dispatcher.py`
**职责**: 分析用户消息，输出 ExecutionPlan（actions[]）
**依赖**: LLMClient, DISPATCHER_PROMPT
**变化**:
- DispatcherDecision 从 `{route, intent, params}` 改为 `{actions: [ActionItem]}`
- Prompt 从"对话方向识别器"改为"对话意图解析器"
- 移除 drug_name 提取职责（交给 react_agent）
- 移除 previous_phase 处理（orchestrator 不再需要）

### 模块 B: ReactAgent（新建）

**文件**: `app/agent/react/agent.py`, `schemas.py`, `tools.py`, `memory.py`
**职责**: 工具驱动的 ReAct 循环，处理所有泛咨询场景
**对外接口**:
```python
class ReactAgent:
    def __init__(self, llm_client, system_prompt, tools, tool_executors, profile, max_iterations=5)
    async def run(self, user_message, conversation_history, context=None) -> AgentResult
```
**依赖**: LLMClient, ToolRegistry, WorkingMemory
**独立性**: 不 import ConversationState，不感知 LangGraph

**ReAct 循环伪代码**:
```python
async def run(self, user_message, history, context):
    messages = build_initial_messages(self.system_prompt, history, user_message, context)
    
    for iteration in range(self.max_iterations):
        response = await self.llm_client.chat.completions.create(
            model=self.profile.model,
            messages=messages,
            tools=self.tool_definitions,    # OpenAI function calling format
            temperature=self.profile.temperature,
        )
        
        if response has tool_calls:
            results = await execute_tools_parallel(response.tool_calls)
            messages.append(response)           # assistant message with tool_calls
            messages.append(format_results(results))  # tool role messages
            record_step(iteration, response.tool_calls, results)
        else:
            return AgentResult(
                final_response=response.content,
                steps=self.steps,
                total_iterations=iteration + 1,
            )
    
    # 超过 max_iterations → 强制总结
    return await force_summarize(messages)
```

### 模块 C: ToolRegistry（新建）

**文件**: `app/agent/react/tools.py`
**职责**: 工具注册、定义管理、执行调度
**对外接口**:
```python
class ToolRegistry:
    def register(self, definition: ToolDefinition, executor: Callable) -> None
    def get_definitions(self) -> list[dict]          # → OpenAI tool format
    def get_executor(self, name: str) -> Callable
    async def execute(self, tool_name: str, arguments: dict) -> ToolResult
```

**5 个工具注册**:
| 工具名 | Executor 绑定 |
|--------|-------------|
| search_drug | `async (query, limit) → await drug_repo.search(query, limit)` |
| get_drug_detail | `async (drug_name) → await drug_repo.get_detail(drug_name) + await retriever.retrieve_multi(drug_name)` |
| search_manual | `async (drug_name, question, top_k) → await retriever.retrieve(drug_name, question, top_k)` |
| get_recommendation | `async () → state.get("recommendations", [])` |
| get_user_profile | `async () → _extract_profile(state.get("consult_slots", {}))` |

**权限**: 全部标记 `capability="read"`。ToolRegistry 在注册时不接受写操作 executor。

### 模块 D: Orchestrator（新建）

**文件**: `app/graph/nodes/orchestrator.py`
**职责**: 读取执行计划，顺序执行 workflow 和 react，组装输出
**依赖**: run_consult, rule_engine, recommend_node, inventory_node, ReactAgent, DB factories
**对外接口**:
```python
async def orchestrator_node(state: ConversationState, ...) -> dict:
    # 返回 {response, phase, node_events, ...}
```

**内部执行逻辑**:
```python
async def orchestrator_node(state, llm_client, rule_engine, drug_repo_factory, 
                             weight_repo_factory, inventory_repo_factory, retriever,
                             scoring_pipeline, react_agent):
    plan = state["dispatcher_result"]["actions"]
    
    workflow_response = ""
    react_response = ""
    workflow_action = None
    
    # ── Step 1: Workflow ──
    workflow_actions = [a for a in plan if a["action"] == "workflow"]
    if workflow_actions:
        wf_action = workflow_actions[0]
        workflow_action = "pending"
        
        # 1a. Consult
        consult_result = await run_consult(llm_client, state["messages"], state["consult_slots"], ...)
        workflow_response = consult_result.response
        
        if consult_result.next_action == "done":
            workflow_action = "done"
            
            # 1b. Safety check
            safety_result = rule_engine.check(consult_result.updated_slots)
            if safety_result.verdict == "BLOCK":
                # 安全拦截 → 跳过推荐，直接返回警告
                return build_response(response=safety_result.message, phase="ended", ...)
            
            # 1c. Recommend + Inventory
            async with drug_repo_factory() as drug_repo, weight_repo_factory() as weight_repo:
                rec_result = await recommend_node(state, llm_client, drug_repo, weight_repo, retriever, scoring_pipeline, ...)
            
            async with inventory_repo_factory() as inv_repo, drug_repo_factory() as drug_repo:
                inv_result = await inventory_node({**state, **rec_result}, inv_repo, drug_repo)
            
            workflow_response = inv_result["response"]  # recommend + inventory 拼接后的完整回复
        else:
            workflow_action = "ask"
    
    # ── Step 2: React ──
    react_actions = [a for a in plan if a["action"] == "react"]
    if react_actions:
        react_action = react_actions[0]
        
        # 构建 context（衔接信息）
        react_context = None
        if workflow_response:
            react_context = {
                "workflow_action": workflow_action,
                "workflow_response": workflow_response,
            }
        
        agent_result = await react_agent.run(
            user_message=react_action.get("query", state.get_last_user_message()),
            conversation_history=state["messages"],
            context=react_context,
        )
        react_response = agent_result.final_response
    
    # ── Step 3: Assemble ──
    if workflow_response and react_response:
        # react_agent 已在其 response 中完成衔接，直接拼接
        final_response = f"{workflow_response}\n\n{react_response}"
    elif workflow_response:
        final_response = workflow_response
    else:
        final_response = react_response
    
    return {
        "response": final_response,
        "phase": "ended",
        "node_events": build_events(plan, workflow_action, agent_result),
        ...
    }
```

### 模块 E: LLMClient（修改）

**文件**: `app/llm/client.py`, `app/llm/profile.py`
**职责**: 支持多 Profile，接受可选的 LLMProfile 参数
**变化**:
- `generate(messages, profile=None)` — profile 为 None 时用默认值
- `generate_structured(messages, schema, profile=None)` — 同上
- 新增 `generate_with_tools(messages, tools, profile=None)` — 供 ReactAgent 使用
- 构造函数不再硬编码 model/temperature，只保存 client 和默认 profile

```python
class LLMClient:
    def __init__(self, settings: Settings):
        self.client = AsyncOpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key)
        self.default_profile = LLMProfile(
            model=settings.llm_model,
            temperature=0.3,
            max_tokens=1024,
        )
    
    async def generate(self, messages, profile: LLMProfile = None, **kwargs):
        p = profile or self.default_profile
        response = await self.client.chat.completions.create(
            model=p.model, messages=messages,
            temperature=p.temperature, max_tokens=p.max_tokens, **kwargs)
        return response.model_dump()
    
    async def generate_with_tools(self, messages, tools, profile=None, **kwargs):
        """单次 LLM 调用，返回可能包含 tool_calls 的响应。不作循环——循环在 ReactAgent 中。"""
        p = profile or self.default_profile
        return await self.client.chat.completions.create(
            model=p.model, messages=messages, tools=tools,
            temperature=p.temperature, max_tokens=p.max_tokens, **kwargs)
```

### 模块 F: Settings（修改）

**文件**: `app/config.py`
**变化**: 新增 4 个 LLMProfile 字段

```python
class Settings(BaseModel):
    # 现有字段保持不变...
    llm_base_url: str
    llm_api_key: str
    llm_model: str = "qwen-plus"  # 默认模型（向后兼容）
    
    # ── 新增: 多 Profile ──
    llm_dispatcher: dict = {"model": "qwen-turbo", "temperature": 0.1, "max_tokens": 256}
    llm_consult: dict = {"model": "qwen-plus", "temperature": 0.3, "max_tokens": 1024}
    llm_react: dict = {"model": "qwen-plus", "temperature": 0.3, "max_tokens": 1024}
    llm_recommend: dict = {"model": "qwen-plus", "temperature": 0.3, "max_tokens": 512}
```

### 模块 G: Graph Builder（修改）

**文件**: `app/graph/builder.py`
**变化**: 图结构从多节点条件边简化为线性管道

```python
def build_graph(...):
    graph = StateGraph(ConversationState)
    
    # 添加节点
    graph.add_node("intake", intake_node)
    graph.add_node("dispatcher", partial(dispatcher_node, llm_client=llm_client, profile=settings.llm_dispatcher))
    graph.add_node("orchestrator", _make_orchestrator(...))  # 所有依赖注入
    graph.add_node("end", _make_end(...))
    
    # 线性边（无条件边）
    graph.set_entry_point("intake")
    graph.add_edge("intake", "dispatcher")
    graph.add_edge("dispatcher", "orchestrator")
    graph.add_edge("orchestrator", "end")
    graph.add_edge("end", END)
    
    return graph.compile()
```

### 模块 H: State（修改）

**文件**: `app/graph/state.py`
**变化**:
- `dispatcher_result` 文档更新为 actions[] 结构
- `phase` 可选取值更新：移除 "explaining"，新增 "reacting"
- 其他字段不变

### 模块 I: 删除的模块

| 文件 | 原因 |
|------|------|
| `app/graph/nodes/explain.py` | 功能被 ReactAgent 吸收 |
| `app/graph/router.py` | 无条件边需要路由，图是线性的 |

## 模块交互

```
用户消息
    │
    ▼
intake_node
    │ (phase="intake")
    ▼
dispatcher_node
    │ (LLM: 解析意图 → actions[])
    │ 输出: dispatcher_result = {actions: [{action, intent, query, priority}]}
    ▼
orchestrator_node
    │
    ├─ 1. has workflow?
    │   └─ YES:
    │       run_consult(messages, slots) → {next_action, response, updated_slots}
    │       │
    │       ├─ next_action == "done":
    │       │   rule_engine.check(slots) → PASS/BLOCK
    │       │   │
    │       │   ├─ BLOCK → response = 就医警告
    │       │   └─ PASS:
    │       │       recommend_node(state, drug_repo, ...) → recommendations + response
    │       │       inventory_node({...rec_result}, inv_repo) → response + 库存
    │       │
    │       └─ next_action == "ask":
    │           response = 追问语（跳过 safety/recommend/inventory）
    │
    ├─ 2. has react?
    │   └─ YES:
    │       react_context = {workflow_action, workflow_response}  (if workflow exists)
    │       react_agent.run(query, history, react_context)
    │       │
    │       │  ┌─ ReAct Loop ──────────────────────────┐
    │       │  │  LLM decide → tool_calls?              │
    │       │  │    → YES: execute tools → append → loop│
    │       │  │    → NO:  final_response               │
    │       │  └────────────────────────────────────────┘
    │       │
    │       └─ AgentResult.final_response
    │
    ├─ 3. Assemble
    │   workflow_response + react_response → final_response
    │   (react_agent 已在输出中处理衔接)
    │
    ▼
end_node
    │ (持久化 messages, 保存 state_snapshot)
    ▼
END
```

## 文件组织

```
app/
  agent/
    prompts.py              # 修改: DISPATCHER_PROMPT 重写
    consult_agent.py        # 不变
    react/                  # 新建目录
      __init__.py           # 导出 ReactAgent, ToolDefinition, AgentResult
      agent.py              # ReactAgent 类
      schemas.py            # ToolDefinition, ToolCall, ToolResult, AgentStep, AgentResult, WorkingMemory
      tools.py              # ToolRegistry 类
      memory.py             # WorkingMemory 管理
  
  graph/
    state.py                # 修改: dispatcher_result 文档, phase 可选取值
    builder.py              # 重写: 简化图结构
    nodes/
      dispatcher.py         # 修改: DispatcherDecision 结构
      orchestrator.py       # 新建: orchestrator_node
      consult.py            # 修改: 函数保留，不再作为图节点
      recommend.py          # 修改: 函数保留（可直接调用）
      inventory.py          # 修改: 函数保留（可直接调用）
      safety_check.py       # 修改: 函数保留（可直接调用）
      explain.py            # 删除
      end.py                # 不变
      intake.py             # 不变
    router.py               # 删除
    
  llm/
    client.py               # 修改: 支持 LLMProfile, 新增 generate_with_tools()
    profile.py              # 新建: LLMProfile 模型

  config.py                 # 修改: 新增 4 个 LLMProfile 字段

specs/agent-architecture-v2/
  spec.md                   # 已完成
  plan.md                   # 本文档
  task.md                   # 下一步
  checklist.md              # 下一步

tests/
  unit/
    test_react_agent.py     # 新建: ReactAgent 单元测试
    test_orchestrator.py    # 新建: Orchestrator 单元测试  
    test_dispatcher_v2.py   # 修改: 适配 actions[] 输出
```

## 技术决策

| 决策点 | 选择 | 理由 |
|--------|------|------|
| Orchestrator 用单节点 vs 多节点图 | **单节点 orchestrator** | workflow 和 react 顺序执行、紧密耦合，react 需要 workflow 输出做衔接。拆成多图节点会导致状态传递复杂 |
| ReactAgent 放在哪 | **`app/agent/react/` 独立模块** | 不依赖 LangGraph，可独立测试、独立演进。未来可被其他入口复用 |
| ReAct 循环实现 | **自己实现，不引入 LangChain Agent** | 不引入新依赖。循环逻辑约 60 行，自己维护成本低 |
| Tool definitions 格式 | **OpenAI function calling 原生格式** | LLMClient 已使用此协议，不需要格式转换 |
| 工具并行执行 | **同一轮多个 tool_calls 并行执行** | 减少延迟。药品查询场景常见"同时查两个药" |
| DB session 管理 | **独立 session，按需开启** | 故障隔离优先。recommend 的 session 故障不影响 inventory。子步骤函数签名不变（继续接受已打开的 session） |
| router.py | **删除，逻辑内化到 orchestrator** | 图变成线性管道，不再需要条件边函数。3 条路由规则的逻辑变为 orchestrator 内部的 if/else，加注释标注"路由点"保证可读性 |
| explain.py | **删除，降级逻辑精髓迁移** | 所有功能被 ReactAgent 的工具覆盖。`_fallback_explain()` 的降级逻辑迁移为 `ReactAgent._format_raw_result()`，在 LLM 不可用时把工具原始数据拼成可读回复 |
| LLMProfile 配置方式 | **Settings 中的 dict，启动时解析为 LLMProfile** | 环境变量可覆盖单个字段，不改代码即可切换模型 |
| 向后兼容 | **State 字段不变，consult/recommend/inventory 核心函数签名不变** | 157 个测试的 fixtures 大部分不用改。graph node 退化为普通函数调用，orchestrator 用 `async with factory()` 管理 session |
