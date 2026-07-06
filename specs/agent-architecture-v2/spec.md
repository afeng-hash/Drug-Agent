# Agent 架构 v2 Spec

## 背景

当前架构经过三轮迭代（症状列表统一、dispatcher 去 recommend、Explain 提示词优化），dispatcher 已经从 4 路由精简到 3 路由（consult / explain / end）。但有两个结构性问题仍未解决：

### 问题 1：Explain 节点设计薄弱，drug_name 提取不可靠

```
用户: "连花清瘟胶囊有什么作用"
  → dispatcher: route="explain" ✅
  → dispatcher: params.drug_name="" ❌  LLM 漏填
  → explain: "请问您想了解哪种药品？" 💀 死胡同
```

更根本的是：dispatcher 被要求同时做路由分类**和**实体提取（drug_name），但实体提取不是它的核心职责。Explain 节点的线性流程（DB → RAG → LLM 模板）也无法处理：

- 药品对比（"布洛芬和对乙酰氨基酚哪个好"）
- 药物相互作用（"这两个能一起吃吗"）
- "这个药"指代（用户刚被推荐了布洛芬，说"这个药伤胃吗"）
- 用户问题驱动的内容聚焦（问副作用却输出禁忌全量）

### 问题 2：用户一句话常包含"症状求药+药品咨询"混合意图

```
用户: "咳嗽吃什么药，布洛芬有什么作用"
```

单路由只能选一个方向——选 consult 则丢药品问题，选 explain/react 则丢症状。dispatcher 不应该做"二选一"，而应该**解析所有意图**。

### 问题 3：LLM Client 绑定单一模型，无法按场景差异化

所有节点共享同一个模型和 temperature，无法让 dispatcher 用快速模型（qwen-turbo）、consult 用准确模型（qwen-plus）、react agent 用推理模型（qwen-plus）。

## 目标

1. **dispatcher 从"单路由"升级为"执行计划"**：解析用户消息的所有意图，输出有序动作列表 `[{action:"workflow",...}, {action:"react",...}]`
2. **引入 ReAct Agent 处理泛咨询**：药品查询、对比、相互作用、闲聊等所有非"症状求药"场景，由 LLM 驱动工具调用自主完成
3. **Explain 节点被 ReAct Agent 吸收**：删除 explain 节点，其功能由 react_agent 的工具（search_drug、search_manual）覆盖
4. **LLM Client 支持多 Profile**：每个场景独立配置模型、temperature、max_tokens，为后续按场景使用不同模型打基础
5. **工具只读**：DB、RAG、KG 对 react_agent 只开放读权限，写操作仅由 workflow 节点完成
6. **混合意图顺序执行**：先跑 workflow（症状求药主链路），再跑 react（泛咨询），react 能拿到 workflow 上下文做自然衔接
7. **输出衔接**：workflow 和 react 的输出不产生断裂感，用模板衔接语拼接为一段自然回复

## 功能需求

### F1: Dispatcher 输出执行计划

Dispatcher 不再输出单一路由，改为输出有序的动作列表 `actions[]`：

**输出结构**：
```
{
  "actions": [
    {"action": "workflow", "intent": "describe_symptom", "priority": 1},
    {"action": "react", "intent": "ask_drug", "query": "布洛芬有什么作用", "priority": 2}
  ]
}
```

**动作类型**：
- `workflow`：症状求药主链路（consult → safety → recommend → inventory）
- `react`：通用对话（药品查询、对比、药物互动、闲聊、放弃等）

**计划编排规则**：
1. 每条用户消息 → 1 或 2 个动作
2. workflow 始终在 react 之前执行（priority: 1 < 2），不并行
3. 纯症状描述 → 只有 workflow
4. 纯药品咨询/闲聊 → 只有 react
5. 混合意图 → [workflow, react]
6. 用户在问诊中回答追问 + 同时问药品 → [workflow, react]

**workflow 意图**：describe_symptom | answer_question | provide_profile | want_recommend | switch_drug | switch_symptom

**react 意图**：ask_drug | compare_drugs | ask_interaction | chat | give_up

**react 的 query 字段**：当 action="react" 时，从用户消息提取具体问题。闲聊时可为空。

### F2: LLM Client 多 Profile（按场景分离）

`LLMClient` 不再绑定单一模型。每个节点/Agent 通过 `LLMProfile` 独立配置：

```python
class LLMProfile(BaseModel):
    model: str           # 模型名，如 "qwen-turbo" / "qwen-plus"
    temperature: float   # 采样温度
    max_tokens: int      # 最大输出 token
    timeout: float       # 超时秒数
```

| 场景 | 模型 | temperature | 理由 |
|------|------|-------------|------|
| dispatcher | qwen-turbo | 0.1 | 解析执行计划，快速+确定性 |
| consult | qwen-plus | 0.3 | 症状提取+结构化输出，需准确 |
| react_agent | qwen-plus | 0.3 | 工具调用+推理，需平衡 |
| recommend | qwen-plus | 0.3 | 文案生成，需自然流畅 |

`LLMClient.generate()` 和 `generate_structured()` 接受可选的 `profile` 参数。不传则用全局默认值（向后兼容）。Profile 通过 `Settings` 类配置，可被环境变量覆盖。

### F3: ReactAgent 独立模块

ReAct Agent 作为一个独立于 LangGraph 节点的模块，放在 `app/agent/react/` 下：

```
app/agent/react/
  __init__.py        # 导出 ReactAgent, ToolDefinition, AgentResult
  agent.py           # ReactAgent 类 — 工具调用循环、错误处理
  schemas.py         # ToolDefinition, ToolCall, AgentStep, AgentResult
  tools.py           # Tool 基类 + ToolRegistry
  memory.py          # WorkingMemory — 本次 agent 调用的工作记忆
```

**核心类**：

```python
class ReactAgent:
    def __init__(
        self,
        llm_client: LLMClient,
        system_prompt: str,
        tools: list[ToolDefinition],
        tool_executors: dict[str, Callable],  # tool_name → async function
        profile: LLMProfile,
        max_iterations: int = 5,
    ): ...

    async def run(
        self,
        user_message: str,
        conversation_history: list[dict],
        context: dict | None = None,  # workflow 上下文
    ) -> AgentResult: ...
```

**AgentResult** 包含：
- `final_response: str` — 最终回复文本
- `steps: list[AgentStep]` — 完整步骤记录（用于 node_events 日志）
- `total_iterations: int` — 总迭代次数
- `total_time_ms: float` — 总耗时

**ReAct 循环**：
1. 构建 messages = [system_prompt, ...conversation_history, user_message]
2. 调用 LLM（带 tool definitions）
3. 如果 LLM 返回 tool_calls → 并行执行工具 → 追加结果到 messages → 回到步骤 2
4. 如果 LLM 返回文本 → 最终回复
5. 超过 max_iterations → 强制 LLM 基于现有信息总结

**独立性**：ReactAgent 不依赖 LangGraph、不 import ConversationState。它只接收 dict/lists。可以被 Graph 节点调用，也可以独立测试。

### F4: 工具集（5 个只读工具）

| # | 工具名 | 签名 | 数据源 | 用途 |
|---|--------|------|--------|------|
| 1 | `search_drug` | `(query: str, limit: int=5) → list[DrugSummary]` | PostgreSQL | 模糊搜索药品（通用名/商品名/拼音） |
| 2 | `get_drug_detail` | `(drug_name: str) → DrugDetail` | PostgreSQL + RAG | 获取药品完整信息（适应症/用法/禁忌/不良反应/相互作用） |
| 3 | `search_manual` | `(drug_name: str, question: str, top_k: int=5) → list[Chunk]` | Milvus | 用用户问题做语义检索，返回说明书相关片段 |
| 4 | `get_recommendation` | `() → list[Recommendation] \| None` | state | 获取当前 session 已推荐的药品列表（解决"这个药"指代） |
| 5 | `get_user_profile` | `() → UserProfile` | state | 获取用户年龄/过敏史/慢性病/特殊人群（个性化回答） |

**权限约束**：全部标记为 `capability=READ`。工具执行不产生任何写操作。Agent System Prompt 中明确约束"你只能查询信息，不能修改任何数据"。

**工具定义复用**：每个工具的 JSON Schema 直接作为 OpenAI function calling 的 `parameters` 传入，不需要二次转换。

### F5: Orchestrator 编排节点

新增 `orchestrator` 节点，替代当前 Graph 中 dispatcher 之后的多路条件边。

**职责**：读取 `execution_plan.actions[]`，按优先级顺序执行 workflow 和 react，然后组装输出。

**内部执行流程**：
```
1. 解析 execution_plan
2. if has workflow action:
     a. 调用 consult（run_consult 函数）
     b. 如果 consult 返回 "done" → 调用 rule_engine.check() → 调用 recommend_node() → 调用 inventory_node()
     c. 如果 consult 返回 "ask" → 跳过 b，直接收集 workflow 输出
3. if has react action:
     a. 构建 react_context：{workflow_action: "done"|"ask", workflow_summary: "..."}
     b. 调用 react_agent.run(query=react_action.query, context=react_context)
4. 调用 assemble() 拼接 workflow 和 react 输出
5. 返回 {response, phase, node_events, ...}
```

**Graph 变化**：
```
改前: intake → dispatcher ──→ consult ──→ safety_block ──→ recommend ──→ inventory ──→ end
                        │    │
                        │    ├── explain ──→ end
                        │    └── end

改后: intake → dispatcher → orchestrator → end
```

删除的节点：`explain`
新增的节点：`orchestrator`
保留的内部调用：`run_consult()`、`rule_engine.check()`、`recommend_node()`、`inventory_node()`（作为 orchestrator 内部调用的函数，不再作为独立图节点）

### F6: 输出衔接（由 React Agent 智能处理）

当 execution plan 同时包含 workflow 和 react 两个 action 时，react agent 的 System Prompt 中动态追加上下文段落，告诉它 workflow 刚才输出了什么，由它自行决定如何自然衔接。

**动态上下文注入**（仅当 workflow 存在时）：
```
## 对话上下文
在你之前，系统的症状问诊流程刚刚完成，已经回复了用户以下内容：
---
{workflow_response}
---

你的回复需要自然地衔接到这段内容之后：
- 不要重复系统已经说过的内容
- 如果系统在追问用户（ask），先简要回应，再过渡到回答用户的新问题
- 如果系统已经给出了推荐（done），直接回答用户的新问题
- 使用自然的过渡语，避免生硬的"另外""此外"
```

**效果示例**：

用户："咳嗽吃什么药，布洛芬有什么作用"

workflow ask（追问中）:
```
workflow: "请问您咳嗽多久了？有没有发烧？"
react:    "好的，关于布洛芬——它是一种解热镇痛药，主要用于退烧和止痛..."
→ 自然，不突兀
```

workflow done（推荐完成）:
```
workflow: "根据您的情况，推荐：1. 右美沙芬（评分85）..."
react:    "您刚才提到的布洛芬，它主要用于退烧和止痛，对干咳帮助不大。如果您主要是咳嗽，右美沙芬会更对症。"
→ react 利用了 workflow 的推荐结果做更精准的回答
```

无 workflow:
```
react: "布洛芬是一种解热镇痛药，主要用于..."
→ 正常独立回复
```

**组装方式**：orchestrator 直接拼接 `workflow_response + "\n\n" + react_response`，衔接过渡已由 react agent 在 `react_response` 内部完成。

### F7: Dispatcher Prompt 重写

适配执行计划输出格式。核心变化：
- 角色从"对话方向识别器"→"对话意图解析器"
- 输出从 `{route, intent, params}` → `{actions: [{action, intent, query, priority}]}`
- 增加混合意图识别规则和示例
- "症状求药"的判断标准和以前一致（描述症状/回答追问/推荐意愿/换药）
- "泛咨询"是兜底——一切不是症状求药的都归为 react

## 非功能需求

### N1: 性能

| 指标 | 目标 |
|------|------|
| Dispatcher LLM 调用 | <300ms（qwen-turbo, 256 tokens） |
| React Agent（简单闲聊） | <500ms（1 次 LLM 调用） |
| React Agent（药品查询，2-3 轮工具调用） | <2s（2-4 次 LLM 调用） |
| Orchestrator 编排开销 | <50ms（纯逻辑，无 IO） |
| 增量延迟（混合意图场景 vs 当前单路由） | 控制在 +1-2s 内 |

### N2: 正确性

- Dispatcher 混合意图识别准确率 ≥ 95%（实测验证，不是形式指标）
- React Agent 工具调用不会修改任何系统状态（DB/RAG/KG 只读）
- Workflow 链路行为不变——consult/safety/recommend/inventory 的输入输出和改前一致

### N3: 健壮性

- React Agent 工具调用失败 → 不中断循环，LLM 看到错误后自行决定重试或告诉用户
- React Agent 超过 max_iterations → 基于已有信息强制总结，返回部分结果
- LLM 不可用 → orchestrator 降级为仅转发错误消息
- 任一环节异常不阻塞另一端（workflow 失败不阻止 react 执行，反之亦然）

### N4: 可观测性

- Dispatcher 输出完整 execution_plan 到 node_events（可审计意图识别质量）
- React Agent 每步 tool_call + tool_result 记录到 node_events（可审计工具使用）
- 最终 response 中不暴露内部工具调用细节（用户只看到自然语言回复）

### N5: 可测试性

- ReactAgent 可脱离 LangGraph 独立单元测试（mock LLM + mock tools）
- Orchestrator 可独立测试（mock workflow + mock react_agent）
- Dispatcher 的 actions[] 输出可用确定性测试用例覆盖

## 不做的事

- ❌ 不做 consult 流程的 tool 化——workflow 保持为独立函数调用，不将 consult 封装为 react_agent 的工具
- ❌ 不做并行执行——workflow 和 react 始终顺序执行，不引入并行异步
- ❌ 不做跨 session 长期记忆——本次只使用 session 级 state 数据
- ❌ 不做 Skills 和 MCP 插件——预留扩展点但不实现
- ❌ 不做前端改动——response 结构不变，对前端透明
- ❌ 不改 consult、safety、recommend、inventory 的内部逻辑——这些节点的核心逻辑作为函数保留
- ❌ 不做工具写权限——所有 react_agent 工具只读
- ❌ 不引入新的外部依赖（如 LangChain Agent、AutoGPT 等）——ReAct 循环自己实现

## 验收标准

- **AC1**: 纯症状描述 → dispatcher 输出 `[{action:"workflow"}]`，走完整 consult 链路
- **AC2**: 纯药品咨询 → dispatcher 输出 `[{action:"react", query:"..."}]`，react_agent 调用工具后回复
- **AC3**: 混合意图（症状+药品）→ dispatcher 输出 `[{action:"workflow"},{action:"react"}]`，顺序执行并自然衔接
- **AC4**: workflow ask 场景 + react 存在 → react_agent 的回复自然衔接到追问后面
- **AC5**: workflow done 场景 + react 存在 → react_agent 的回复利用 workflow 推荐结果做更精准回答
- **AC6**: "这个药"指代 → react_agent 通过 get_recommendation 工具解析，正确回答
- **AC7**: 药品对比（"布洛芬和对乙酰氨基酚哪个好"）→ react_agent 分别查询两个药品后对比回复
- **AC8**: 闲聊/感谢/放弃 → react_agent 不需要工具，自然回复
- **AC9**: Dispatcher 输出格式为 `{actions: [{action, intent, query, priority}]}` schema
- **AC10**: LLMProfile 分离——每个场景独立配置模型/温度/token，环境变量可覆盖
- **AC11**: ReactAgent 模块可独立测试——不依赖 LangGraph，mock LLM + mock tools
- **AC12**: 工具只读——react_agent 的 5 个工具不产生写操作
- **AC13**: Graph 中 explain 节点删除，新增 orchestrator 节点
- **AC14**: 所有现有测试通过（157 个），consult/safety/recommend/inventory 行为不变
