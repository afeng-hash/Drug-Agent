# 药品评分排序模块 (Drug Scorer) Plan

## 架构概览

```
                       ┌─────────────────────────────┐
                       │      recommend_node          │
                       │  (orchestrator, not scorer)  │
                       └────────────┬────────────────┘
                                    │
            ┌───────────────────────┼───────────────────────┐
            │                       │                       │
            ▼                       ▼                       ▼
    ┌───────────────┐     ┌─────────────────┐     ┌─────────────────┐
    │   Evidence    │     │  WeightConfig   │     │    Scorer       │
    │   Engine      │     │  (PostgreSQL)   │     │    Engine       │
    │               │     │                 │     │                 │
    │ 规则1 规则2 ..│     │ weights表       │     │ Σ(wᵢ × fᵢ)    │
    │  ↓       ↓    │     │ versions表      │     │ 排序 → Top-K    │
    │ feature值     │     │ A/B 路由        │     │ 明细输出        │
    └───────┬───────┘     └────────┬────────┘     └────────┬────────┘
            │                      │                        │
            │        features      │     weights            │
            ▼                      ▼                        ▼
    ┌──────────────────────────────────────────────────────────────┐
    │                    ScoringPipeline                            │
    │  for each drug:                                              │
    │    features = evidence_engine.evaluate(slots, drug)           │
    │    result   = scorer.score(features, weights)                 │
    │  ranked = sort(drugs, key=result.total_score, reverse=True)   │
    │  return ranked[:top_k] + scoring_details                      │
    └──────────────────────────────────────────────────────────────┘
```

**三层的物理分布：**

| 层 | 位置 | 形式 |
|----|------|------|
| Evidence 规则 | `app/scorer/evidence/*.py` | Python 类，每个规则一个文件 |
| Feature 向量 | 内存 | `dict[str, float]` |
| Weight 配置 | PostgreSQL `weights_config` 表 | 结构化行数据 |
| Scoring 引擎 | `app/scorer/engine.py` | 纯函数 |

## 核心数据结构

### FeatureVector

```python
# 特征向量 —— Evidence 规则写入，Scorer 读取
# 就是一个 float dict，维度名作为 key
FeatureVector = dict[str, float]
# 示例: {"symptom_match": 0.85, "safety": 0.95, "age_suitability": 0.70, ...}
```

### Evidence

```python
from abc import ABC, abstractmethod

class BaseEvidence(ABC):
    """一条证据规则的基类。"""

    @property
    @abstractmethod
    def feature_name(self) -> str:
        """本证据影响哪个特征维度，如 'symptom_match'"""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """可读描述，用于解释：如 '症状关键词匹配药品适应症'"""
        ...

    @abstractmethod
    def evaluate(self, slots: dict, drug: Drug) -> EvidenceResult:
        """评估本条证据，返回对特征值的贡献。
        
        同一 feature 可被多条证据修改（取最不利值、最大值等，由 merge 策略决定）。
        """
        ...
```

### EvidenceResult

```python
@dataclass
class EvidenceResult:
    feature_name: str        # 影响的特征维度
    value: float             # 0.0 ~ 1.0
    reason: str              # 面向人类的解释，如 "适应症包含'头痛'，与用户症状匹配"
    merge_strategy: str      # "max" | "min" | "avg" | "set"
    # max: 多条证据中取最大值（如症状匹配——哪个症状命中都算匹配）
    # min: 多条证据中取最小值（如安全性——任何一条禁忌命中即不安全）
    # avg: 取平均值
    # set: 直接覆盖
```

### ScoredDrug

```python
@dataclass
class DimensionScore:
    """单个维度的得分明细。"""
    feature_name: str          # 维度名，如 "symptom_match"
    weight: float              # 权重 w
    feature_value: float       # 特征值 f
    contribution: float        # w × f
    evidence_reasons: list[str] # 贡献此得分的证据描述列表

@dataclass
class ScoredDrug:
    """一个药品的完整评分结果。"""
    drug_id: int
    generic_name: str
    total_score: float                    # Σ(wᵢ × fᵢ)
    dimensions: list[DimensionScore]      # 各维度明细
    excluded: bool                         # 是否被安全降级排除
    exclude_reason: str = ""               # 排除原因

@dataclass
class ScoringResult:
    """一次评分管线的完整输出。"""
    drugs: list[ScoredDrug]           # 按 total_score 降序排列
    config_version: str               # 使用的权重版本号
    total_time_ms: float              # 总耗时
```

### 权重配置 DB 模型

```python
class WeightConfig(Base):
    """权重配置表。"""
    __tablename__ = "weights_config"

    id: int                       # 自增主键
    version: str                  # 语义版本号 "v3.2.1"，唯一
    policy: str                   # 策略名 "balanced" | "safety_first"
    weights: dict                 # JSON: {"symptom_match": 0.30, "safety": 0.25, ...}
    feature_defaults: dict        # JSON: 特征默认值 {"symptom_match": 0.0, "safety": 1.0, ...}
    safety_block_threshold: float # safety 低于此值排除
    is_active: bool               # 当前是否激活
    ab_group: str | None          # A/B 分组标识，"A" / "B" / None(全量)
    ab_ratio: float | None        # A/B 流量比例 0.0~1.0
    description: str              # 变更说明
    changed_by: str               # 操作人
    created_at: datetime
```

### Strategy

```python
@dataclass
class StrategyConstraint:
    """策略层的约束定义。"""
    name: str                            # "safety_first" | "balanced"
    constraints: dict[str, tuple[float, float]]
    # {"safety": (0.35, None), "symptom_match": (None, 0.30)}
    # (min, max), None 表示不限制该方向

    def validate(self, weights: dict[str, float]) -> bool:
        """校验权重配置是否满足策略约束。"""
        ...
```

## 模块设计

### 模块 A: Evidence 规则集 (`app/scorer/evidence/`)

**职责**：将 `(consult_slots, Drug)` 转化为特征值。每条证据独立、可测试、可组合。

**对外接口**：
- `BaseEvidence.evaluate(slots, drug) → EvidenceResult`

**依赖**：仅依赖 `Drug` ORM 模型（读取字段）和 `consult_slots` dict（读取症状）。不依赖 DB、不依赖网络、不依赖其他 Evidence。

**初始 7 条证据规则**：

| 类名 | feature_name | 逻辑 | merge |
|------|-------------|------|-------|
| `SymptomKeywordMatch` | symptom_match | 症状名在适应症文本中出现 → 1.0；部分匹配 → 0.5；无匹配 → 0.0 | max |
| `SymptomSeverityMatch` | symptom_match | 有发热 + 药品有退热成分 → 0.3 加成 | max |
| `ContraindicationCheck` | safety | 禁忌症命中 → 0.0；慎用命中 → 0.3；无冲突 → 1.0 | min |
| `AllergyCheck` | safety | 过敏史命中药品成分 → 0.0；无过敏 → 1.0 | min |
| `AgeSuitability` | age_suitability | 成人(12-60) → 1.0；老人(60+)有老人用法 → 0.8；儿童有儿童用法 → 0.7 | min |
| `OtcSafetyLevel` | otc_safety_level | 乙类 OTC → 1.0；甲类 OTC → 0.7 | set |
| `IngredientCoverage` | ingredient_coverage | 症状数 / 被覆盖的症状数 | max |

**merge 策略说明**：
- `max`（症状匹配）：多个症状匹配同一个药，取最好的一次匹配
- `min`（安全性、年龄适配）：木桶原理，最不利的证据决定该维度得分
- `set`（OTC 等级）：直接覆盖，无多条证据竞争

### 模块 B: EvidenceEngine (`app/scorer/evidence_engine.py`)

**职责**：管理 Evidence 注册表，执行全部证据规则，应用 merge 策略，输出最终 FeatureVector。

**对外接口**：
- `register(evidence: BaseEvidence) → None`：注册一条证据规则
- `evaluate(slots, drug) → FeatureVector`：执行全部规则 → 返回特征向量
- `evaluate_with_detail(slots, drug) → tuple[FeatureVector, list[EvidenceResult]]`：同上 + 保留所有证据明细（用于解释）

**核心逻辑**：
```
1. 初始化 feature 值 = 默认值（从 WeightConfig.feature_defaults 取）
2. for each registered evidence:
       result = evidence.evaluate(slots, drug)
       按 result.merge_strategy 更新 feature[result.feature_name]
3. 返回最终 feature dict
```

### 模块 C: ScoringEngine (`app/scorer/engine.py`)

**职责**：纯函数，`FeatureVector × Weights → ScoredDrug`。

**对外接口**：
- `score_one(features, weights, drug_id, drug_name) → ScoredDrug`
- `score_all(drugs, features_list, weights) → ScoringResult`

**核心逻辑**：
```
for each drug:
    if features['safety'] < safety_block_threshold:
        → 标记 excluded=True
    else:
        for each dimension in weights:
            contribution = weight × feature_value
        total_score = Σ contributions
    sort by total_score desc
    return top_k + scoring details
```

**约束**：
- 无 IO、无随机数、无 datetime 调用
- 权重自动归一化：`weight = weight / sum(all_weights)` 确保总和为 1.0
- 输入 features 和 weights 的 key 必须一致（不一致的维度忽略或告警）

### 模块 D: WeightConfigRepository (`app/db/repositories/weight_config.py`)

**职责**：从 DB 读取权重配置，支持 TTL 缓存和 A/B 路由。

**对外接口**：
- `get_active(session_id: str) → WeightConfig`：根据 session 分桶返回当前激活的配置
- `get_version(version: str) → WeightConfig`：按版本号查询
- `list_versions() → list[WeightConfig]`：所有历史版本

**A/B 分桶逻辑**：
```
bucket = hash(session_id) % 100
if config.ab_group == "A" and bucket < config.ab_ratio × 100:
    return config_A
elif config.ab_group == "B":
    return config_B
# 全量
return active_config
```

**TTL 缓存**：
- 首次读取或过期后从 DB 查询
- 默认 TTL 60 秒
- 使用模块级 `_cache: dict[str, tuple[float, WeightConfig]]` + `_cache_time`

### 模块 E: StrategyValidator (`app/scorer/strategy.py`)

**职责**：校验权重配置是否满足策略约束。

**对外接口**：
- `validate(weights: dict, strategy_name: str) → tuple[bool, str]`：返回 (是否通过, 失败原因)

**内置两个策略**：

| 策略 | 约束 |
|------|------|
| `balanced` | symptom_match: [0.25, 0.35], safety: [0.20, 0.30], age_suitability: [0.15, 0.25] |
| `safety_first` | safety: [0.35, 0.50], symptom_match: [0.10, 0.30], age_suitability: [0.15, 0.25] |

### 模块 F: ScoringPipeline (`app/scorer/pipeline.py`)

**职责**：一键编排，暴露给 `recommend_node` 的唯一入口。

**对外接口**：
- `run(candidates, slots, session_id) → ScoringResult`

**内部流程**：
```
1. weight_config = weight_repo.get_active(session_id)
2. strategy_validator.validate(weight_config.weights, weight_config.policy)
3. for each drug in candidates:
       features = evidence_engine.evaluate(slots, drug)
   → features_list
4. result = scoring_engine.score_all(candidates, features_list, weight_config)
5. return result
```

## 模块交互

```
recommend_node
    │
    ├── 1. DB 查候选药品 (drug_repo)  ← 不变
    ├── 2. 安全规则排除 (safety_result.excluded_drugs) ← 不变
    │
    ├── 3. 🆕 scoring_pipeline.run(candidates, slots, session_id)
    │       │
    │       ├── weight_config_repo.get_active(session_id)
    │       │     └── DB: SELECT * FROM weights_config WHERE is_active=true
    │       │         + A/B 分桶: hash(session_id) % 100
    │       │
    │       ├── strategy_validator.validate(weights, policy)
    │       │
    │       ├── for each drug:
    │       │     evidence_engine.evaluate(slots, drug)
    │       │       ├── SymptomKeywordMatch.evaluate()
    │       │       ├── ContraindicationCheck.evaluate()
    │       │       ├── AllergyCheck.evaluate()
    │       │       ├── AgeSuitability.evaluate()
    │       │       ├── OtcSafetyLevel.evaluate()
    │       │       └── IngredientCoverage.evaluate()
    │       │     → FeatureVector (dict[str, float])
    │       │
    │       └── scoring_engine.score_all()
    │             └── s = Σ(w × f) → ScoredDrug
    │
    ├── 4. ScoredDrug → 格式化回复 (RAG + DB) ← RAG 检索不变
    └── 5. 返回 response
```

**数据流图**：

```
consult_slots ──┐
                ├──→ EvidenceEngine ──→ FeatureVector ──┐
drug (ORM)  ────┘                                       ├──→ ScoringEngine ──→ ScoredDrug[]
session_id ────→ WeightRepo ──→ WeightConfig ──────────┘
```

## 文件组织

```
app/scorer/
├── __init__.py                 — 公开导出: ScoringPipeline, ScoredDrug, ...
├── engine.py                   — ScoringEngine 纯函数: score_one(), score_all()
├── evidence_engine.py          — EvidenceEngine: register(), evaluate()
├── pipeline.py                 — ScoringPipeline: 编排入口 run()
├── strategy.py                 — StrategyValidator + 内置策略定义
├── schemas.py                  — 所有 dataclass: ScoredDrug, ScoringResult, ...
└── evidence/
    ├── __init__.py              — 从各文件导入 BaseEvidence
    ├── base.py                  — BaseEvidence ABC, EvidenceResult
    ├── symptom_keyword.py       — SymptomKeywordMatch
    ├── symptom_severity.py      — SymptomSeverityMatch
    ├── contraindication.py      — ContraindicationCheck
    ├── allergy.py               — AllergyCheck
    ├── age_suitability.py       — AgeSuitability
    └── ingredient_coverage.py   — IngredientCoverage, OtcSafetyLevel

app/db/repositories/
├── weight_config.py             — 🆕 WeightConfigRepository (TTL cache + A/B)

app/db/models.py                 — 🆕 新增 WeightConfig ORM 模型

app/graph/nodes/recommend.py     — 🔧 用 scoring_pipeline 替代 LLM 排序
app/graph/builder.py             — 🔧 _make_recommend 注入 scoring_pipeline

tests/
├── unit/test_evidence/           — 🆕 每条 Evidence 规则的独立单测
│   ├── test_symptom_keyword.py
│   ├── test_contraindication.py
│   ├── test_allergy.py
│   ├── test_age_suitability.py
│   └── test_ingredient_coverage.py
├── unit/test_evidence_engine.py  — 🆕 合并策略测试
├── unit/test_scoring_engine.py   — 🆕 纯函数测试（最重要的单测）
├── unit/test_weight_config.py    — 🆕 A/B 路由 + TTL 缓存测试
└── unit/test_strategy.py         — 🆕 策略约束校验测试
```

## 技术决策

| # | 决策点 | 选择 | 理由 |
|---|--------|------|------|
| D1 | FeatureValue 用 float 而非 int | `float 0.0~1.0` | 证据有强度差异（精确匹配 1.0 vs 部分匹配 0.5），int 区分度不够 |
| D2 | Evidence 注册方式 | 代码注册 `engine.register(Evidence())` | 简单直观。后期可改为 entry_points 自动发现 |
| D3 | 权重归一化 | 自动 `w / Σw` | 运营配权重时不需要手工凑到 1.0，降低配置出错概率 |
| D4 | A/B 分桶方式 | `hash(session_id) % 100` | 同一 session 的多次请求稳定在同一桶，避免用户看到前后矛盾的结果 |
| D5 | 安全排除 vs 降序 | safety < 阈值 → **排除**（不参与排序） | 药店场景下安全是硬约束。禁忌症命中 → 绝对不推荐，不是「不太推荐」 |
| D6 | Evidence 不调用 LLM | 纯规则匹配 | 确定性要求：相同输入 → 相同得分。LLM 的结果不可复现 |
| D7 | 权重配置存储 | PostgreSQL（唯一来源） | 用户明确不需要降级链。单点 DB 足够稳定 |
| D8 | TTL 缓存粒度 | 60 秒 / 模块级变量 | 平衡时效性和 DB 压力。60 秒对运营配置足够实时 |
| D9 | merge 策略 | 按 Evidence 声明（max/min/avg/set） | 不同维度的合并逻辑本质不同：症状匹配取最优，安全取最差。统一策略反而是错的 |
| D10 | LLM 保留角色 | 只生成推荐理由文案 | 排序是数学问题，解释是语言问题。分工明确，各司其职 |
