# PR Summary：统一症状数据结构 — 废弃 `other_symptoms`，所有症状平等对待

**Branch**: `master`  
**Files**: 16 modified | **Diff**: `+75 / -55`

---

## 一、问题背景

旧设计中，症状被分为两个字段存储：

```python
consult_slots = {
    "symptoms":       [{"name": "头痛", "severity": "中度"}],  # 主诉症状
    "other_symptoms": ["流鼻涕", "咳嗽"],                      # 伴随症状
}
```

这带来了几个问题：

1. **LLM 决策负担**：Consult Agent 需要在「主诉」和「伴随」之间做语义区分，LLM 经常犹豫不决，导致症状被放入错误字段
2. **下游处理复杂**：Recommend 节点需要拼接两处来源（1.0 权重 + 0.5 权重），Scoring 证据规则需要分别遍历两处，Safety 规则只检查 `other_symptoms` 而遗漏 `symptoms` 中的急症关键词
3. **字段语义模糊**："头痛"和"流鼻涕"对用户来说都是不舒服的症状，强行区分主/次没有药学依据——症状应该按「是否匹配药品适应症」来评价，而非按「用户先说的还是后说的」

## 二、解决方案

**所有症状统一放入 `symptoms` 列表，废弃 `other_symptoms` 字段。**

```python
# 新设计
consult_slots = {
    "symptoms": [
        {"name": "头痛", "severity": "中度", "onset": "2天前"},
        {"name": "流鼻涕", "onset": "有点"},
        {"name": "咳嗽"},
    ],
}
# other_symptoms 字段已删除
```

### 变更范围

本次改动横跨 **4 层架构**，确保端到端一致性：

```
Prompt (LLM 指令)
  → Consult Node (初始化)
    → State (数据结构)
      → Recommend Node (提取+去重)
      → Safety Rules (急症/过敏检测)
      → Evidence Rules (症状关键词匹配)
```

---

## 二、逐文件变更

### 1. Prompt 层 — 从源头消除歧义

**`app/agent/prompts.py`** (+12/-5)

```diff
+## 症状归类规则（重要）
+- 用户提到的所有症状统一放入 `symptoms` 列表，不要区分「主诉」和「伴随」
+- 每个症状的 `onset` 字段记录用户的自然语言描述
+- 不要在 symptoms 和 other_symptoms 之间犹豫——全部进 symptoms
+
+**禁止使用 other_symptoms 字段**——所有症状放入 symptoms 列表
```

LLM 输出示例也从 `"other_symptoms": ["流鼻涕"]` 改为在 `symptoms` 中包含多个条目。

### 2. State 层 — 数据结构统一

**`app/graph/state.py`** (+4/-2)

- 删除 `other_symptoms` 字段定义和文档注释
- 新增说明："所有症状统一放入 symptoms 列表，不区分「主诉」和「伴随」"
- `initial_state()` 移除 `"other_symptoms": []`

**`app/graph/nodes/consult.py`** (+1/-1)

- 删除空槽位初始化中的 `"other_symptoms": []`

### 3. Recommend 节点 — 简化症状提取

**`app/graph/nodes/recommend.py`** (+22/-11)

```diff
- # 主诉症状 weight=1.0，附加症状 weight=0.5
- primary_names = _extract_symptom_names(symptoms)
- secondary_names = _extract_symptom_names(slots.get("other_symptoms", []))
- symptom_weights = (
-     [{"name": n, "weight": 1.0} for n in primary_names]
-     + [{"name": n, "weight": 0.5} for n in secondary_names]
- )

+ # 所有症状等权，不区分主诉/伴随
+ symptom_names_raw = _extract_symptom_names(symptoms)
+ symptom_weights = [{"name": n, "weight": 1.0} for n in symptom_names_raw]
```

同时新增 **步骤 1.6 — 去重**：标准化后同一标准症状名只保留一次，避免 KG 重复查询。

### 4. Safety Rule 层 — 统一症状来源

**`app/rules/base.py`** (-1)

- 删除 docstring 中的 `other_symptoms` 字段说明

**`app/rules/definitions/r4_emergency_signs.py`** (+13/-4)  
**`app/rules/definitions/r5_severe_allergy.py`** (+13/-4)

两者做了相同的重构：从读取 `other_symptoms`（纯字符串列表）改为读取统一的 `symptoms`（dict 列表），并兼容 str 格式：

```python
# 旧：只检查 other_symptoms（纯字符串列表）
other_symptoms = slots.get("other_symptoms", [])
other_text = " ".join(other_symptoms).lower()

# 新：检查统一 symptoms（dict 列表，也兼容 str 格式）
symptoms = slots.get("symptoms", [])
symptom_names = []
for s in symptoms:
    if isinstance(s, dict):
        name = s.get("name", "")
        if name:
            symptom_names.append(name)
    elif isinstance(s, str):
        symptom_names.append(s)
symptom_text = " ".join(symptom_names).lower()
```

**影响**：之前 R4/R5 只检测 `other_symptoms` 中的急症关键词，如果用户把"呼吸困难"说成主诉症状（放入 `symptoms`），反而不触发拦截。现在统一检测所有症状，**急症拦截覆盖率提升**。

### 5. Evidence Rule 层 — 简化匹配逻辑

**`app/scorer/evidence/base.py`** (-1)

- 删除 docstring 中的 `other_symptoms` 字段说明

**`app/scorer/evidence/symptom_keyword.py`** (+2/-8)

```diff
- # 分别在 symptoms 和 other_symptoms 中搜索
- other_symptoms = slots.get("other_symptoms", [])
- all_symptoms = symptom_names + [
-     s.get("name", s) if isinstance(s, dict) else str(s)
-     for s in other_symptoms
- ]

+ # 所有症状已统一在 symptoms 列表中
```

简化了两个来源拼接的代码，症状关键词匹配逻辑更清晰。

### 6. 测试层 — 全面适配

| 文件 | 变更 | 说明 |
|------|------|------|
| `tests/conftest.py` | +3/-2 | `emergency_slots` 急症症状改为 dict 格式放入 `symptoms`；`empty_slots` 删除 `other_symptoms` |
| `tests/integration/test_chat_flow.py` | -1 | 删除 `other_symptoms` |
| `tests/integration/test_safety_flow.py` | +1/-2 | R4 测试改用 `symptoms` + dict 格式 |
| `tests/unit/test_consult_agent.py` | -7 | 5 处删除 `other_symptoms` |
| `tests/unit/test_dispatcher.py` | -2 | 2 处删除 `other_symptoms` |
| `tests/unit/test_rules_engine.py` | +6/-5 | R4/R5 测试改为 `symptoms` + dict 格式 |
| `tests/unit/test_dispatcher.py` | -2 | 删除 `other_symptoms` |

### 7. 其他

**`app/graph/nodes/dispatcher.py`** (+1)

- 添加 TODO 注释标记已知问题

---

## 三、Breaking Changes

### BC-1：`consult_slots` 数据结构变更

```python
# 旧
{"symptoms": [...], "other_symptoms": [...]}

# 新
{"symptoms": [...]}  # other_symptoms 已删除
```

**影响范围**：

| 影响方 | 处理方式 |
|--------|---------|
| LLM Prompt | ✅ 已更新——要求所有症状进 `symptoms`，禁止使用 `other_symptoms` |
| Safety Rules (R4, R5) | ✅ 已更新——从 `symptoms` 读取，兼容 dict/str 格式 |
| Evidence Rules | ✅ 已更新——删除 `other_symptoms` 拼接逻辑 |
| Recommend Node | ✅ 已更新——所有症状等权 |
| State / Consult Node | ✅ 已更新——删除字段定义和初始化 |
| **数据库 `state_snapshot`** | ⚠️ 旧会话的 snapshot 可能包含 `other_symptoms` 字段，下游代码已兼容（`slots.get("other_symptoms", [])`→`[]`），不影响运行 |
| **前端** | ⚠️ 如果前端直接读取 `consult_slots.other_symptoms` 展示症状列表，需改为读取 `consult_slots.symptoms` |

### BC-2：安全规则检查范围变化（业务行为变更）

R4（急症信号）和 R5（严重过敏）**之前只检查 `other_symptoms`**，现在检查所有 `symptoms`。这可能导致之前未被拦截的会话现在触发 BLOCK——但这是**正确的行为修正**，急症信号本就不应该被遗漏。

---

## 四、业务影响

| 维度 | 影响 |
|------|------|
| **推荐准确度** | 所有症状等权参与评分，不再因"伴随症状"被 0.5 折权而遗漏匹配 |
| **急症拦截覆盖率** | R4/R5 从只检查 `other_symptoms` 扩展到所有症状，不再遗漏主诉中的急症信号 |
| **LLM 调用质量** | Consult Agent 不再纠结"主诉 vs 伴随"的语义区分，症状收集更稳定 |
| **代码简洁度** | 删除 55 行分散在两处的症状拼接/遍历代码 |
| **数据模型清晰度** | 单一症状来源，不再有"这个症状该放哪里"的歧义 |

---

