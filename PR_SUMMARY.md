# PR Summary：Dispatch 职责边界重构 — 收窄 Dispatcher，强化 Consult Agent

**Branch**: `master`  
**Files**: 7 modified | **Diff**: `+239 / -83`

---

## 一、问题背景

旧架构中，Dispatcher 同时承担两个职责：

1. **对话方向分类**（consult / explain / recommend / end）——合理
2. **隐式判断信息充分性**——越界了

这导致两个问题：

**问题 A — Dispatcher 越权推荐**：
用户说"推荐吧"，Dispatcher 直接 route="recommend" 跳过 consult。此时如果用户消息中还夹杂了"我12岁"这样的信息，年龄就被丢掉了——因为 Dispatcher 不提取症状，只是做路由决策。

**问题 B — Consult Agent 信息不全**：
Consult Agent 不知道上游 Dispatcher 的意图判断（用户是"想推荐了"还是"想换药"），无法针对性调整追问策略。同时也不知道系统上一轮问了什么，面对用户的简短回答（"没有""38度"）缺乏上下文。

## 二、解决方案

**一句话**：Dispatcher 只做对话方向分类（3 路），推荐永远是 consult→done 的自然结果；Consult Agent 成为推荐流程的唯一守门人。

```
旧架构：
  Dispatcher ──recommend──▶ SafetyBlock ──▶ Recommend
       │                         ↑
       └──consult──▶ Consult      │  （Dispatcher 可以绕过 Consult 直接推荐）
                                     信息提取不完整

新架构：
  Dispatcher ──consult──▶ Consult ──done──▶ SafetyBlock ──▶ Recommend
       │                     ↑
       └──explain            │  （推荐只有一条路径：Consult 判定 done）
       └──end                │  Consult 收到 intent 上下文，可针对性追问
```

### 变更范围

```
Dispatcher (职责收窄)
  → Router (3 路分支)
    → Consult Node (上下文增强)
      → Consult Agent (Prompt 大幅强化)
        → State (注释更新)
```

---

## 三、逐文件变更

### 1. `app/agent/prompts.py` (+114/-27) — 核心重构

**Dispatcher Prompt 重写**：从"调度器"改名为"对话方向识别器"，核心定位从"决定系统下一步做什么"改为"判断用户当前在说什么"。

关键变化：
- 新增"核心约束"声明：不判断信息是否充分、不决定是否可以推荐
- 路由从 4 类简化为 3 类（移除 recommend），按优先级组织
- 意图分类从 6 种扩展到 9 种：新增 `answer_question`、`provide_profile`、`want_recommend`
- 新增 7 条决策规则（信息充分性不是你的事、简短应答不是结束、混合意图看主次等）
- 新增反模式列表（不要因为"推荐"就 route="recommend"、不要根据 slots 判断够了等）
- "换药也是 consult"——之前 switch_drug 会路由到 recommend，现在走 consult

**Consult Prompt 重写**：从 ~30 行扩展到 ~120 行。

关键变化：
- **两级信息充分标准**：第一级（必须）—至少 1 个症状；第二级（尽力获取）—持续时间/年龄/过敏/慢性病
- **追问策略**：一次只问 1 个，优先问未获取维度，已问过的别重复
- **4 种特殊场景处理**：
  - 用户表达推荐意愿（`want_recommend`）：提取信息→判断→不够追问 1 个关键问题
  - 用户要求换药（`switch_drug`）：有症状→直接 done；没有→正常问诊
  - 用户回答否定：关联上一轮提问的维度，记录否认
  - 用户不耐烦：只要第一级满足→done
- **槽位定义**：JSON 格式说明每个字段的含义和 null 的语义
- **症状提取规则**：明确否定也要记录、onset 保留用户原话

### 2. `app/agent/consult_agent.py` (+49/-13)

**新增 3 个参数**：

```python
async def run_consult(
    ...,
    dispatcher_intent: str = "",       # 新增
    dispatcher_params: dict | None = None,  # 新增
    last_question: str = "",           # 新增
)
```

**上下文消息重构**：从简单字符串拼接改为结构化 markdown（4 个 section）：

```python
# 旧
context_msg = {"role": "system", "content": f"## slots\n{current_slots}\n## 轮数\n{rounds}"}

# 新 — 结构化
context_parts = [
    "## 当前已收集的症状信息 (slots)", "```json", json.dumps(slots), "```",
    "## 进度", f"- 已追问轮数: {rounds}/{max}",
    "## 上游路由意图", f"dispatcher_intent = \"{intent}\"",
    "## 上一轮系统提问", f"系统刚才问了: \"{question}\"",
]
```

### 3. `app/graph/nodes/consult.py` (+27/-2)

**新增 `_extract_last_assistant_question()`** — 从对话历史提取系统最近一次提问，帮助 LLM 理解"用户当前在回答什么"。

**新增 3 个参数传递**：从 state 提取 `dispatcher_intent` 和 `last_question`，传递给 `run_consult()`。

### 4. `app/graph/nodes/dispatcher.py` (+8/-2)

- `DispatcherDecision` schema 更新：route 移除 recommend，intent 新增 3 种
- 移除 `#todo` 注释（问题已通过架构重构解决）
- 降级路由注释更新

### 5. `app/graph/builder.py` (+3/-8)

- Graph 图注释更新：recommend 不再是 dispatcher 的直接分支
- 条件边从 4 路缩减为 3 路（移除 `"recommend": "safety_block"`）

### 6. `app/graph/router.py` (+2/-4)

- 白名单从 `{"consult", "explain", "recommend", "end"}` 缩减为 `{"consult", "explain", "end"}`
- 注释更新

### 7. `app/graph/state.py` (+8/-8)

- `dispatcher_result` 字段注释更新：意图从 6 种扩展为 9 种，消费方从 `consult/explain/recommend` 改为 `consult/explain`
- 明确标注：Dispatcher 不判断信息充分性，recommend 路由已移除

---

## 四、Breaking Changes

### BC-1：Dispatcher 路由从 4 路缩减为 3 路（业务行为变更）

```python
# 旧：4 路
{"consult", "explain", "recommend", "end"}

# 新：3 路
{"consult", "explain", "end"}
```

**影响**：

| 场景 | 旧行为 | 新行为 | 影响 |
|------|--------|--------|------|
| 用户说"推荐吧" | Dispatcher → recommend → 直接推荐 | Dispatcher → consult → Consult 判定 done → 推荐 | 多走一轮 consult，但信息提取更完整 |
| 用户说"有没有便宜的" | Dispatcher → recommend（带 filter_cheaper） | Dispatcher → consult → Consult 判定 done → 推荐 | 换药参数仍通过 intent=switch_drug 传递 |
| 用户在 consulting 阶段说"推荐吧"且附带"我12岁" | 年龄信息丢失（Dispatcher 不提取） | Consult 提取年龄后再 done | **信息不丢失** |

**核心原则**：推荐永远是 consult→done 的自然结果，Dispatcher 没有直达推荐的权利。这确保了信息提取的完整性。

### BC-2：`run_consult()` 新增 3 个可选参数（向后兼容）

```python
async def run_consult(
    ...,
    dispatcher_intent: str = "",          # 新增，默认空
    dispatcher_params: dict | None = None, # 新增，默认 None
    last_question: str = "",              # 新增，默认空
)
```

默认值保证向后兼容。但如果外部有直接调用 `run_consult()` 的代码（测试除外），建议传入新参数以获得更好的上下文感知。

---

## 五、业务影响

| 维度 | 影响 |
|------|------|
| **信息提取完整度** | 用户说"推荐吧"时附带的信息（年龄、过敏等）不再丢失，Consult 会先提取再 done |
| **追问精准度** | Consult Agent 知道上游意图（want_recommend vs switch_drug vs normal），可针对性调整策略 |
| **上下文感知** | 知道上一轮问了什么，面对"没有""38度"等简短回答时不再困惑 |
| **职责边界清晰** | Dispatcher 只管分类，Consult 管提取+判定，各司其职，不会互相越界 |
| **Prompt 可维护性** | 结构化 markdown 上下文替代字符串拼接，后续扩展更方便 |
| **系统安全性** | 推荐只有一条路径（consult→done），不会因 Dispatcher 判断失误跳过信息收集 |

---


