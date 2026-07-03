# 药品评分排序模块 (Drug Scorer) Tasks

## 文件清单

| 操作 | 文件 | 职责 |
|------|------|------|
| 新建 | `app/scorer/__init__.py` | 公开导出：ScoringPipeline, ScoredDrug, ScoringResult |
| 新建 | `app/scorer/schemas.py` | 所有 dataclass：ScoredDrug, DimensionScore, ScoringResult, EvidenceResult |
| 新建 | `app/scorer/evidence/base.py` | BaseEvidence ABC + EvidenceResult |
| 新建 | `app/scorer/evidence/__init__.py` | 汇聚导出所有 Evidence 规则 |
| 新建 | `app/scorer/evidence/symptom_keyword.py` | SymptomKeywordMatch 证据规则 |
| 新建 | `app/scorer/evidence/symptom_severity.py` | SymptomSeverityMatch 证据规则 |
| 新建 | `app/scorer/evidence/contraindication.py` | ContraindicationCheck 证据规则 |
| 新建 | `app/scorer/evidence/allergy.py` | AllergyCheck 证据规则 |
| 新建 | `app/scorer/evidence/age_suitability.py` | AgeSuitability 证据规则 |
| 新建 | `app/scorer/evidence/ingredient_coverage.py` | IngredientCoverage + OtcSafetyLevel 证据规则 |
| 新建 | `app/scorer/evidence_engine.py` | EvidenceEngine：注册 + 执行 + merge |
| 新建 | `app/scorer/engine.py` | ScoringEngine：纯函数 score_one() + score_all() |
| 新建 | `app/scorer/strategy.py` | StrategyValidator + balanced/safety_first 策略定义 |
| 新建 | `app/scorer/pipeline.py` | ScoringPipeline：编排入口 run() |
| 新建 | `app/db/repositories/weight_config.py` | WeightConfigRepository：DB 查询 + TTL + A/B |
| 修改 | `app/db/models.py` | 新增 WeightConfig ORM 模型 |
| 修改 | `app/graph/nodes/recommend.py` | 用 scoring_pipeline 替代 LLM 排序调用 |
| 修改 | `app/graph/builder.py` | _make_recommend 注入 scoring_pipeline |
| 新建 | `tests/unit/test_evidence/test_symptom_keyword.py` | |
| 新建 | `tests/unit/test_evidence/test_contraindication.py` | |
| 新建 | `tests/unit/test_evidence/test_allergy.py` | |
| 新建 | `tests/unit/test_evidence/test_age_suitability.py` | |
| 新建 | `tests/unit/test_evidence/test_ingredient_coverage.py` | |
| 新建 | `tests/unit/test_evidence_engine.py` | |
| 新建 | `tests/unit/test_scoring_engine.py` | |
| 新建 | `tests/unit/test_weight_config.py` | |
| 新建 | `tests/unit/test_strategy.py` | |
| 新建 | `tests/unit/test_pipeline.py` | |

---

## T1: 创建数据模型 (schemas + Evidence 基类)

**文件：** `app/scorer/schemas.py`, `app/scorer/evidence/base.py`, `app/scorer/__init__.py`
**依赖：** 无
**步骤：**
1. 创建 `app/scorer/` 目录结构
2. 在 `schemas.py` 中定义：`DimensionScore`, `ScoredDrug`, `ScoringResult`（dataclass）
3. 在 `schemas.py` 中定义：`EvidenceResult`（dataclass）
4. 在 `evidence/base.py` 中定义：`BaseEvidence` 抽象基类（ABC），包含 `feature_name`(property), `description`(property), `evaluate(slots, drug) → EvidenceResult`(abstract)
5. 在 `__init__.py` 中公开导出核心类

**验证：** `python -c "from app.scorer import ScoredDrug, ScoringResult, BaseEvidence; print('OK')"`

---

## T2: 实现 7 条 Evidence 规则

**文件：** `app/scorer/evidence/*.py`（6 个文件）
**依赖：** T1
**步骤：**
1. `symptom_keyword.py`：症状名在 `drug.indication_summary` 中 ILIKE 匹配。精确命中 → 1.0；部分命中 → 0.5；未命中 → 0.0。merge=`max`。
2. `symptom_severity.py`：有发热症状 + 药品含退热成分（布洛芬/对乙酰氨基酚/金刚烷胺）→ +0.3 贡献。merge=`max`。
3. `contraindication.py`：从 `slots.chronic_conditions` 和 RAG 获取的禁忌信息匹配药品 → 命中禁忌 → 0.0；命中慎用 → 0.3；无冲突 → 1.0。merge=`min`。
4. `allergy.py`：`slots.allergies` 与 `drug.active_ingredients` 交叉匹配。命中 → 0.0；无过敏 → 1.0。merge=`min`。
5. `age_suitability.py`：判断用户年龄段 → 成人(12-60)有用法 → 1.0；老人(60+)有 `usage_elderly` → 0.8、无 → 0.5；儿童有 `usage_child` → 0.7、无 → 0.3。merge=`min`。
6. `ingredient_coverage.py`：`IngredientCoverage`：症状数 vs 适应症覆盖数 → 全覆盖 1.0、部分 0.5、无 0.0。merge=`max`。`OtcSafetyLevel`：乙类 → 1.0、甲类 → 0.7。merge=`set`。

**验证：** 每条规则单独导入无报错：
`python -c "from app.scorer.evidence.symptom_keyword import SymptomKeywordMatch; print('OK')"`

---

## T3: 实现 EvidenceEngine

**文件：** `app/scorer/evidence_engine.py`
**依赖：** T1, T2
**步骤：**
1. 实现 `register(evidence: BaseEvidence)` 方法（内部 `list` 存储）
2. 实现 `evaluate(slots, drug) → FeatureVector` 方法：
   - 遍历所有已注册 Evidence
   - 按 merge_strategy 更新 feature 值
   - merge 逻辑：`set` 直接覆盖；`max` 取 `max(old, new)`；`min` 取 `min(old, new)`；`avg` 追加到列表最后求平均
3. 提供默认 feature 初始值字典（在 engine 构造函数中传入或使用 DEFAULT_DEFAULTS）
4. 实现 `evaluate_with_detail(slots, drug) → tuple[FeatureVector, list[EvidenceResult]]`

**验证：** `python -c "from app.scorer.evidence_engine import EvidenceEngine; e = EvidenceEngine(); print(len(e._rules))"` 无报错

---

## T4: 实现 ScoringEngine（纯函数）

**文件：** `app/scorer/engine.py`
**依赖：** T1（仅依赖 schemas）
**步骤：**
1. 实现 `score_one(features, weights, drug_id, drug_name, threshold) → ScoredDrug`：
   - 归一化 weights：`w = w / sum(weights.values())`
   - 遍历每个维度：`contribution = weights[key] × features.get(key, 0.0)`
   - 计算 total_score
   - safety < threshold → excluded=True
   - 返回 ScoredDrug 含 DimensionScore 明细
2. 实现 `score_all(drugs, features_list, weights, threshold) → ScoringResult`：
   - 批量计算每个 drug 的得分
   - 按 total_score 降序排列
   - excluded=True 的排在最后或移除
3. 确保函数无 IO、无随机数、无全局状态

**验证：** 单测 T24 通过（最关键的测试）

---

## T5: 实现 StrategyValidator

**文件：** `app/scorer/strategy.py`
**依赖：** 无
**步骤：**
1. 定义内置策略：
   - `balanced`：symptom_match (0.25, 0.35), safety (0.20, 0.30), age_suitability (0.15, 0.25)
   - `safety_first`：safety (0.35, 0.50), symptom_match (0.10, 0.30), age_suitability (0.15, 0.25)
2. 实现 `validate(weights, strategy_name) → tuple[bool, str]`：
   - 遍历 strategy 的每条约数 → 检查 weight 是否在 (min, max) 范围内
   - min=None 表示不限制下限，max=None 表示不限制上限
3. 实现 `list_strategies() → list[str]`

**验证：** `python -c "from app.scorer.strategy import StrategyValidator; v = StrategyValidator(); ok, _ = v.validate({'symptom_match': 0.3, 'safety': 0.4, 'age': 0.2}, 'safety_first'); assert ok; print('OK')"`

---

## T6: 新增 WeightConfig ORM 模型

**文件：** `app/db/models.py`
**依赖：** 无
**步骤：**
1. 新增 `WeightConfig` SQLAlchemy 模型：
   - 表名 `weights_config`
   - 字段：id, version(unique), policy, weights(JSON), feature_defaults(JSON), safety_block_threshold, is_active, ab_group, ab_ratio, description, changed_by, created_at

**验证：** `python -c "from app.db.models import WeightConfig; print('OK')"`

---

## T7: 实现 WeightConfigRepository

**文件：** `app/db/repositories/weight_config.py`
**依赖：** T6
**步骤：**
1. 实现 `get_active(session_id) → WeightConfig`：
   - 检查 TTL 缓存（60 秒过期 + 模块级变量）
   - 缓存未命中 → DB 查询 `is_active=true` 的配置
   - A/B 分桶：`hash(session_id) % 100 < ab_ratio*100` → 返回对应分组
   - 更新缓存时间戳
2. 实现 `get_version(version) → WeightConfig`
3. 实现 `list_versions() → list[WeightConfig]`
4. 实现 `set_active(version)` 和 `insert(config)`

**验证：** 单测 T26 通过

---

## T8: 实现 ScoringPipeline（编排入口）

**文件：** `app/scorer/pipeline.py`
**依赖：** T3, T4, T5, T7
**步骤：**
1. 实现 `run(candidates, slots, session_id) → ScoringResult`：
   - `weights = weight_repo.get_active(session_id)`
   - `strategy_validator.validate(weights.weights, weights.policy)`
   - 遍历 candidates → `evidence_engine.evaluate(slots, drug)` → features_list
   - `scoring_engine.score_all(candidates, features_list, weights)`
   - 返回 ScoringResult
2. 记录总耗时 `total_time_ms`
3. 异常处理：任何一步失败 → 降级为简单排序（按 OTC 等级 + 字母序）

**验证：** `python -c "from app.scorer.pipeline import ScoringPipeline; print('OK')"`

---

## T9: 接入 recommend_node

**文件：** `app/graph/nodes/recommend.py`, `app/graph/builder.py`
**依赖：** T8
**步骤：**
1. 在 `recommend_node` 中删除 LLM 排序代码（`_rank_drugs` 调用）
2. 替换为 `scoring_pipeline.run(candidates, slots, session_id)`
3. 从 `ScoringResult` 中提取 top 1-3 个药品
4. LLM 只调用一次用于生成推荐理由文案（可选，也可以从 Evidence.reason 拼装）
5. 回复格式中使用 ScoredDrug 的维度明细
6. `builder.py` 中 `_make_recommend` 注入 `scoring_pipeline`

**验证：** 验收测试 E2E-1 通过，推荐结果不依赖 LLM 排序

---

## T10-T17: 单元测试 — Evidence 规则

**文件：** `tests/unit/test_evidence/*.py`（5 个文件）
**依赖：** T2

| Task | 文件 | 测试对象 |
|------|------|---------|
| T10 | test_symptom_keyword.py | 精确命中 / 部分命中 / 未命中 / merge=max |
| T11 | test_symptom_severity.py | 发热+退热成分 / 无发热 / merge=max |
| T12 | test_contraindication.py | 禁忌命中→0.0 / 慎用→0.3 / 无冲突→1.0 / merge=min |
| T13 | test_allergy.py | 成分过敏→0.0 / 无过敏→1.0 / merge=min |
| T14 | test_age_suitability.py | 儿童/成人/老人 + 有/无对应用法 / merge=min |
| T15 | test_ingredient_coverage.py | 全覆盖/部分覆盖/无覆盖 + OTC等级 |

**验证：** `pytest tests/unit/test_evidence/ -v` 全部通过

---

## T18: 单元测试 — EvidenceEngine

**文件：** `tests/unit/test_evidence_engine.py`
**依赖：** T3, T10-T15
**步骤：**
1. 测试 register 后 evaluate 用到了注册的规则
2. 测试多个规则影响同一 feature 时的 merge 策略正确性
3. 测试 evaluate_with_detail 返回完整明细
4. 测试空规则列表时不崩溃

**验证：** `pytest tests/unit/test_evidence_engine.py -v` 全部通过

---

## T19: 单元测试 — ScoringEngine

**文件：** `tests/unit/test_scoring_engine.py`
**依赖：** T4
**步骤：**
1. 测试权重归一化：输入权重总和≠1.0 → 自动归一化
2. 测试得分计算：给定 features + weights → 验证 total_score 计算正确
3. 测试安全排除：safety < threshold → excluded=True
4. 测试排序：3 个药按 total_score 降序排列
5. 测试空输入：0 个药品 → 返回空 ScoringResult
6. 测试确定性：相同输入两次 → 相同输出
7. 性能测试：12 个药品 < 15ms

**验证：** `pytest tests/unit/test_scoring_engine.py -v` 全部通过

---

## T20: 单元测试 — StrategyValidator

**文件：** `tests/unit/test_strategy.py`
**依赖：** T5
**步骤：**
1. 测试 balanced 策略：合规权重通过
2. 测试 safety_first 策略：safety < 0.35 的权重被拒绝
3. 测试未知策略名报错
4. 测试边界值（精确等于 min/max）

**验证：** `pytest tests/unit/test_strategy.py -v` 全部通过

---

## T21: 单元测试 — WeightConfigRepository

**文件：** `tests/unit/test_weight_config.py`
**依赖：** T7
**步骤：**
1. Mock DB → 测试 get_active 返回正确配置
2. 测试 TTL 缓存：第一次查 DB，第二次走缓存
3. 测试 A/B 分桶：给定 session_id → 稳定在同一组
4. 测试缓存过期后重新查 DB

**验证：** `pytest tests/unit/test_weight_config.py -v` 全部通过

---

## T22: 单元测试 — ScoringPipeline

**文件：** `tests/unit/test_pipeline.py`
**依赖：** T8, T19-T21
**步骤：**
1. Mock 所有依赖 → 测试 pipeline.run() 返回 ScoringResult
2. 测试异常降级：DB 不可用时返回简单排序
3. 测试 pipeline 输出的 config_version 与配置一致

**验证：** `pytest tests/unit/test_pipeline.py -v` 全部通过

---

## T23: 数据迁移 — 创建默认权重配置

**文件：** `data/seed.py`（追加）
**依赖：** T6
**步骤：**
1. 在 seed.py 中添加初始化 `weights_config` 表逻辑
2. 插入默认 balanced 配置：
   ```json
   {"symptom_match": 0.30, "safety": 0.25, "age_suitability": 0.20, 
    "otc_safety_level": 0.10, "ingredient_coverage": 0.10, "evidence_quality": 0.05}
   ```

**验证：** `python data/seed.py` → DB 中 weights_config 表有一条记录

---

## T24: 全量回归测试

**文件：** 所有 tests/
**依赖：** T9, T23
**步骤：**
1. 运行全部单元测试：`pytest tests/unit/ -v`
2. 运行全部集成测试：`pytest tests/integration/ -v`
3. 运行验收测试：`python tests/acceptance.py`
4. 确认 40+ 测试全部通过

**验证：** 所有测试通过，LLM 不再参与排序

---

## 执行顺序

```
T1 ──→ T2 ──→ T3 ──→ T8 ──→ T9 ──→ T24
         │       │       ↑
         │       │   T4 ─┘
         │       │   T5 ─┘
         │       │   T7 ─┘ (需 T6)
         │       │
         ├── T10-T15 (并行，随 T2)
         │
         ▼
    T16 ← T3
         T17 ← T4
         T18 ← T5
         T19 ← T7 + T6
         T20 ← T8
         
T6 ──→ T7 ──→ T19
T6 ──→ T23 (data/seed.py)
```

可并行执行的组：
- T4 (ScoringEngine) 与 T2 (Evidence 规则) 并行
- T5 (Strategy) 与 T2 并行
- T6 (ORM 模型) 与 T1-T5 并行
- T10-T15 (Evidence 单测) 与各自规则写完后并行
