# PR Summary：症状标准化模块 — 自由文本 → KG 标准名两级匹配

**Branch**: `master`  
**Files**: 8 changed (4 modified + 4 new) | **Diff**: `+34 / -2`（不含新文件）  
**New code**: `app/normalizer/` (~650 行) + `tests/unit/test_symptom_normalizer.py` (~700 行)

---

## 一、问题背景

用户在对话中描述症状时，用词千差万别——"喉咙不舒服""嗓子疼""咽部不适"可能都指向 KG 中的标准名「咽喉痛」。在此之前，推荐节点直接将用户原始文本传给 KG 查询，自由文本与标准症状名不匹配会导致：

- KG 查不到对应 Symptom 节点 → 候选药品缺失
- 症状匹配证据规则评分偏低 → 推荐不精准

## 二、解决方案

新增 **症状标准化模块** `app/normalizer/`，在推荐节点评分前将用户自由文本症状名映射到 KG 标准症状词表。

### 架构

```
用户输入 "喉咙不舒服" 
    │
    ▼
┌─────────────────────────────────────────────────────┐
│  SymptomNormalizer.normalize(["喉咙不舒服", "干咳"]) │
│                                                      │
│  Layer 0 (确定性, <1ms):                              │
│    ① exact 匹配 → "干咳" 命中 ✓ (1.0置信度)           │
│    ② alias 匹配 → "喉咙不舒服" → 别名表 → 未命中       │
│    ③ contains 匹配 → "喉咙不舒服" contains "咽喉" → 未命中│
│                                                      │
│  Layer 1 (LLM + 硬词表约束):                          │
│    "喉咙不舒服" → LLM → "咽喉痛"                       │
│    → 硬词表验证(在词表中 ✓) → 风险分层(L1 ≥0.7 ✓)     │
│    → 接受 (0.75置信度)                                 │
│                                                      │
│  输出: ["咽喉痛", "干咳"]                              │
└─────────────────────────────────────────────────────┘
    │
    ▼
推荐节点用标准化后的名称查 KG
```

### 模块结构

| 文件 | 职责 |
|------|------|
| `app/normalizer/__init__.py` | 模块导出 |
| `app/normalizer/schemas.py` | `NormalizedSymptom`（单个映射结果）+ `NormalizationResult`（批量汇总） |
| `app/normalizer/symptom_normalizer.py` | **核心引擎**：两级匹配 + 风险分层 + LLM 缓存 |
| `app/normalizer/vocabulary.py` | `VocabularySource` 抽象接口 + `Neo4jVocabularySource` 实现 |

### Layer 0 — 确定性匹配（零 LLM 开销）

| 策略 | 说明 | 置信度 |
|------|------|--------|
| **exact** | 完全匹配标准名 | 1.0 |
| **alias** | 匹配别名表（如「嗓子疼」→「咽喉痛」） | 1.0 |
| **contains** | 双向包含匹配（如「一直干咳」contains「干咳」），取最长命中 | 0.80 |

### Layer 1 — LLM 语义映射（仅在 Layer 0 未命中时触发）

- **硬词表约束**：LLM 返回的标准名必须在词表中存在，否则丢弃
- **风险分层**（基于 KG 症状层级）：
  - Level 1（粗粒度，如「发热」）：置信度 ≥ 0.7 → 接受
  - Level 2（中等粒度，如「干咳」）：置信度 ≥ 0.85 → 接受
  - Level 3（细粒度，如「右下肢放射痛」）：不走 LLM，直接丢弃
- **结果缓存**：同一 raw_text 不重复调 LLM

### 词表加载 — `Neo4jVocabularySource`

- 启动时一次性从 Neo4j 拉取全部 Symptom 节点 + IS_A 关系
- 构建 `name→SymptomEntry` 和 `alias→name` 两个内存索引
- 运行时零 Neo4j 开销
- 如果 Neo4j 不可用，词表为空，标准化步骤跳过

---

## 三、变更文件

### 新建

| 文件 | 行数 | 说明 |
|------|------|------|
| `app/normalizer/__init__.py` | 21 | 模块导出 |
| `app/normalizer/schemas.py` | 30 | `NormalizedSymptom` + `NormalizationResult` |
| `app/normalizer/symptom_normalizer.py` | 384 | 核心：两级匹配 + 风险分层 + LLM 缓存 |
| `app/normalizer/vocabulary.py` | 155 | `VocabularySource` 抽象 + `Neo4jVocabularySource` |
| `specs/symptom-normalization/` | 4 文件 | spec.md / plan.md / task.md / checklist.md |
| `tests/unit/test_symptom_normalizer.py` | ~700 | 完整单元测试 |

### 修改

| 文件 | 变更 | 说明 |
|------|------|------|
| `app/main.py` | +7 | 启动时创建 `Neo4jVocabularySource`、加载词表、挂载到 `app.state`、传给 graph builder |
| `app/graph/builder.py` | +6/-2 | `build_graph()` 和 `_make_recommend()` 新增 `vocab_source` 参数 |
| `app/graph/nodes/recommend.py` | +22 | 新增步骤 1.5：症状标准化。在 KG 查询之前将自由文本症状名映射为标准名，同时保留原始名 `_raw_name` 供调试 |
| `app/agent/consult_agent.py` | +1 | 注释标记（TODO） |

---

## 四、Breaking Changes

### BC-1：`build_graph()` 和 `_make_recommend()` 新增参数

```python
# 旧
def build_graph(..., drug_graph_repo=None, max_consult_rounds=6)
def _make_recommend(..., drug_graph_repo=None)

# 新
def build_graph(..., drug_graph_repo=None, vocab_source=None, max_consult_rounds=6)
def _make_recommend(..., drug_graph_repo=None, vocab_source=None)
```

`vocab_source` 有默认值 `None`，**向后兼容**。不传时症状标准化步骤自动跳过。

### BC-2：`recommend_node()` 新增可选参数

```python
async def recommend_node(..., drug_graph_repo=None, vocab_source=None)
```

同理，默认值保证兼容。

---

## 五、业务影响

| 影响维度 | 说明 |
|----------|------|
| **推荐精准度提升** | 用户说的口语化症状名称被标准化为 KG 标准名，KG 查询命中率提高 |
| **LLM 调用量可控** | Layer 0 覆盖大部分常见表达（exact/alias/contains），仅少数未命中才走 LLM；结果缓存避免重复调用 |
| **安全兜底** | 细粒度症状（Level 3）不走 LLM，避免错误映射导致错误推荐；LLM 返回的结果经硬词表验证后才接受 |
| **Neo4j 不可用不受影响** | 词表加载失败时 `vocab_source=None`，标准化步骤跳过，推荐管线正常运作 |
| **启动时间略增** | 每次启动加载一次词表（~100 条症状），毫秒级 |

---


