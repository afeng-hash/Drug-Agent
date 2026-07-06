# PR Summary：Dispatcher v2 执行计划 + ReactAgent 替代 Explain 节点

**Branch**: `master`  
**Files**: 11 files changed | **Diff**: `+780 / -502`  
**Key**: `app/graph/nodes/explain.py` — **deleted** (138 lines)

---

## 一、概述

本次变更是架构 v2 升级的第二步（第一步 `f49cdca` 已完成 Dispatcher 职责收窄）：

1. **Dispatcher 从「单路由」升级为「执行计划」** — 支持混合意图，一次输出 1~2 个有序动作
2. **ReactAgent（工具驱动的 ReAct 循环）替代旧 Explain 节点** — 药品查询从模板化解释升级为多工具协同的智能回复
3. **LLM 多 Profile 支持** — 每个业务节点使用独立的模型配置（而非共用一个全局 model）

```
旧图：
  intake → dispatcher → {consult, explain, end}
  consult → {safety_block, end}
  safety_block → {recommend, end}
  recommend → inventory → end
  explain → end

新图：
  intake → dispatcher → {consult, react}
  consult → {safety_block, react, end}
  safety_block → {recommend, end}
  recommend → inventory → {react, end}
  react → end
```

核心变化：explain 节点被 react 替代；react 成为所有路径的汇聚点。

---

## 二、逐文件变更

### 1. `app/agent/prompts.py` (+192/-86) — Dispatcher Prompt 重写 + 新增 ReactAgent Prompt

**Dispatcher Prompt 升级为 v2**：

- 名称从"对话方向识别器"升级为"对话意图解析器（v2: 执行计划格式）"
- 输出从 `{route, intent, params}` 变为 `{actions: [{action, intent, query, priority}]}`
- 路由分类从 3 路（consult/explain/end）变为 2 种动作类型（workflow/react）
- 新增加编排规则（6 条）、2 种意图分类（workflow + react 各有独立意图集）
- 新增 6 个详细示例（纯症状、纯药品、混合意图、闲聊、回答追问+药品、换药）
- 移除 `previous_phase` 输入字段（Dispatcher 不再维护此概念）

**新增 REACT_SYSTEM_PROMPT**（~35 行）：
- 定义 ReactAgent 的能力边界（查药、对比、相互作用、解析指代、闲聊）
- 工具使用规则（并行调用、继续探索、通俗解释）
- 权限约束（只读、不处方、仅供参考）
- "这个药"指代处理指南

### 2. `app/graph/nodes/dispatcher.py` (+127/-138) — 执行计划格式

**Schema 变更**：

```python
# 旧
class DispatcherDecision:
    route: str    # "consult" | "explain" | "end"
    intent: str   # 9 种意图
    params: dict  # {drug_name, reset_slots, ...}

# 新
class ActionItem:
    action: str    # "workflow" | "react"
    intent: str    # 按 action 类型分流
    query: str     # react 动作的核心问题
    priority: int  # 1=先执行

class DispatcherDecision:
    actions: list[ActionItem]  # 1~2 个有序动作
```

**关键变化**：
- `previous_phase` 跳转逻辑完全移除（v2 不再需要）
- `node_events` 从 `{route, intent}` 变为 `{actions: [{action, intent, priority}]}`
- `dispatcher_result` 从 `{route, intent, params}` 变为 `{actions: [...]}`
- 降级策略：从"默认走 consult"变为"默认走 react"（安全兜底）

### 3. `app/graph/nodes/explain.py` — **已删除**（-138 lines）

旧的 explain 节点被 ReactAgent 完全替代。旧节点只能：DB 查药 → Milvus RAG → LLM 模板输出。新 ReactAgent 可以：多工具并行调用、药品对比、指代解析、上下文衔接。

### 4. `app/graph/builder.py` (+396/-208) — 图结构重构 + ReactAgent 工厂

**核心变化**：

| 维度 | 旧 | 新 |
|------|----|----|
| explain 节点 | 存在 | **删除** |
| react 节点 | 不存在 | **新增**（汇聚点） |
| dispatcher 分支 | 3 路（consult/explain/end） | 2 路（consult/react） |
| consult 分支 | 2 路（safety_block/end） | 3 路（safety_block/react/end） |
| inventory 分支 | 直接→end | 条件分支（react/end） |

**新增类 `_StateProxy`**：可变的 state 代理，ReactAgent 的 `get_recommendation` / `get_user_profile` 工具通过它读取 state 数据，react_node 每次调用前更新。

**新增 `_make_react()` 工厂**：注册 5 个工具 → 实例化 ReactAgent → 绑定 react_node。

**新增 6 个工具 Executor 工厂**（`_make_search_drug`, `_make_get_drug_detail`, `_make_search_manual`, `_make_get_recommendation`, `_make_get_user_profile`），每个工厂返回闭包 executor，内部自行管理 DB session 生命周期。

### 5. `app/graph/router.py` (+88/-57) — 基于 actions[] 的路由

所有路由函数从读取 `dispatcher_result.route` 改为读取 `dispatcher_result.actions[]`：

| 函数 | 逻辑 |
|------|------|
| `route_after_dispatcher` | 有 workflow action → "consult"；纯 react → "react" |
| `route_after_consult` | done → "safety_block"；ask + 有 react → "react"；ask → "end" |
| `route_after_safety` | 不变 |
| `route_after_inventory` | **新增** — 有 react → "react"；无 → "end" |

新增 `_get_actions()` 辅助函数。

### 6. `app/llm/client.py` (+139/-65) — 多 Profile + generate_with_tools

**多 Profile 支持**：所有 4 个方法（`generate`, `generate_structured`, `generate_with_tools`, `stream`）新增 `profile: LLMProfile | None` 参数。未传入时使用 `self.default_profile`（向后兼容）。

**新增 `generate_with_tools()`**：单次 LLM 调用，返回原始响应对象（含可能的 tool_calls）。专供 ReactAgent 的 ReAct 循环使用——每次只做一次请求，循环逻辑由 ReactAgent 控制。

**实现细节**：
- `self.model` → `self.default_profile = LLMProfile(model=settings.llm_model)`
- temperature/max_tokens 从 `float`/`int` 变为 `Optional[...]`，None 时取 profile 默认值
- `generate_structured()` 的 JSON 模式和 tool-call 回退两种路径都使用 profile

### 7. `app/config.py` (+34) — 多 Profile 配置

```python
llm_dispatcher: dict = {"model": "qwen-turbo", "temperature": 0.1, "max_tokens": 256}
llm_consult: dict    = {"model": "qwen-plus",  "temperature": 0.3, "max_tokens": 1024}
llm_react: dict      = {"model": "qwen-plus",  "temperature": 0.3, "max_tokens": 1024}
llm_recommend: dict  = {"model": "qwen-plus",  "temperature": 0.3, "max_tokens": 512}
```

新增 `get_profile(field_name)` 方法，从 dict 构建 `LLMProfile` 对象。支持通过环境变量覆盖（如 `LLM_DISPATCHER='{"model":"qwen-turbo","temperature":0}'`）。

### 8. `app/graph/state.py` (+50/-42) — 注释和文档更新

- Phase 枚举：`explaining` → `reacting`
- `previous_phase` 注释重写为"预留字段"
- `dispatcher_result` 注释从 route 格式完全重写为 actions[] 格式
- 各字段的"消费"标注从具体文件改为架构角色（如"Orchestrator"）

### 9. `app/graph/nodes/consult.py` (+1) — 注释标记

添加 `#todo` 标记（重构提示）。

### 10. 测试文件

- `tests/unit/test_dispatcher.py` (+99/-56)：所有测试用例从 route 格式迁移到 actions[] 格式；新增 `test_dispatcher_mixed_intent`（混合意图测试）；fallback 断言更新
- `tests/integration/test_chat_flow.py` (+18/-12)：节点清单从 explain→react；topic_switch 测试注释更新

---

## 三、核心架构决策

### 决策 1：Dispatcher 输出执行计划而非单一路由

**旧问题**：用户说"咳嗽吃什么药，布洛芬有什么作用"——Dispatcher 只能选一条路（consult 或 explain），另一条信息被丢弃。

**新方案**：Dispatcher 输出 `[{workflow, priority=1}, {react, priority=2}]`，Orchestrator 按优先级顺序执行。Workflow 先跑（consult→recommend→inventory），然后 react 拿推荐结果做药品解释。

### 决策 2：ReactAgent 替代 Explain 节点

| 维度 | 旧 Explain | 新 ReactAgent |
|------|-----------|---------------|
| 数据来源 | DB + RAG（单药） | DB + RAG + State（多药/对比） |
| 交互模式 | 单次 LLM 调用 | ReAct 多轮工具循环 |
| 药品对比 | 不支持 | 支持（并行调用两个 get_drug_detail） |
| "这个药"指代 | 依赖 dispatcher 提取 drug_name | 通过 get_recommendation 工具自动解析 |
| 上下文衔接 | 无 | 感知 workflow 状态，自然过渡 |
| 并行工具调用 | 不支持 | 支持（同一轮调用多个工具） |

### 决策 3：多 Profile 分离模型配置

Dispatcher 用快速模型（qwen-turbo, temperature=0.1），Consult/React 用准确模型（qwen-plus, temperature=0.3）。未来可独立调节每个节点的模型而互不影响。

---

## 四、Breaking Changes

### BC-1：`dispatcher_result` 结构完全变更

```python
# 旧格式
state["dispatcher_result"] = {"route": "consult", "intent": "describe_symptom", "params": {}}

# 新格式
state["dispatcher_result"] = {
    "actions": [
        {"action": "workflow", "intent": "describe_symptom", "priority": 1}
    ]
}
```

**影响范围**：
- 所有消费 `dispatcher_result` 的代码（router.py、consult_node、react_node、chat.py SSE 事件）
- 所有测试用例
- 数据库 session 的 `state_snapshot` 字段中存储的历史 dispatcher_result

### BC-2：`explain` 节点已删除

旧 API 中依赖 explain 节点的任何路径（如旧版 Dispatcher 的 route="explain"）不再存在。所有药品查询统一走 ReactAgent。

### BC-3：LLMClient 构造签名变更

```python
# 旧
self.model = settings.llm_model

# 新
self.default_profile = LLMProfile(model=settings.llm_model)
```

`self.model` 属性不再存在。直接访问 `client.model` 的代码会报 AttributeError。

### BC-4：generate* 方法的 temperature/max_tokens 类型变更

```python
# 旧
generate(messages, temperature: float = 0.3, max_tokens: int = 1024)

# 新
generate(messages, temperature: float | None = None, max_tokens: int | None = None, profile=None)
```

如果外部代码用位置参数传递 temperature/max_tokens，行为不变。但如果依赖"不传参数时默认为 0.3/1024"的语义，现在默认值来自 profile。

---

## 五、业务影响

| 维度 | 影响 |
|------|------|
| **药品查询能力** | 从单药模板解释升级为多工具协同（查药+详情+说明书+推荐列表+用户画像） |
| **混合意图处理** | 用户"咳嗽吃什么药，顺便问布洛芬副作用"——两个意图都被正确执行 |
| **模型资源优化** | Dispatcher 用轻量模型（省钱+快），Consult/React 用准确模型 |
| **"这个药"指代** | ReactAgent 通过 get_recommendation 工具自动解析，不再依赖上游提取 drug_name |
| **代码复杂度** | explain 节点删除（-138 lines），ReactAgent 以工具注册方式扩展功能 |
| **可配置性** | 每个节点的 model/temperature/max_tokens 可通过环境变量独立覆盖 |

---
