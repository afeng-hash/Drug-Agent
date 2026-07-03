# 药品评分排序模块 (Drug Scorer) Checklist

> 每一项通过运行代码或观察行为来验证，聚焦系统行为。

## 实现完整性

- [ ] C1: `app/scorer/` 目录存在且包含所有 8 个模块文件（验证：`ls app/scorer/*.py | wc -l` ≥ 8）
- [ ] C2: `app/scorer/evidence/` 目录存在且包含 base.py + 6 条规则文件（验证：`ls app/scorer/evidence/*.py | wc -l` ≥ 7）
- [ ] C3: `app/db/repositories/weight_config.py` 存在（验证：文件可导入）
- [ ] C4: `app/db/models.py` 中包含 `WeightConfig` 类（验证：`from app.db.models import WeightConfig`）
- [ ] C5: `recommend_node` 不再调用 `_rank_drugs` 或任何 LLM 排序方法（验证：grep `_rank_drugs` 在 recommend.py 中无结果）

## 核心功能

- [ ] C6: EvidenceEngine 注册 7 条规则后可正常执行 evaluate（验证：给定测试 slots + drug → 返回 6 维 FeatureVector）
- [ ] C7: 症状"头痛发热" + 布洛芬 → symptom_match > 0（验证：单测输出 > 0.5）
- [ ] C8: 过敏史含"布洛芬" + 药品为布洛芬 → safety = 0.0（验证：AllergyCheck 单测）
- [ ] C9: 用户年龄 8 岁 + 药品无儿童用法 → age_suitability < 0.5（验证：AgeSuitability 单测）
- [ ] C10: ScoringEngine 接收 features + weights → 返回 ScoredDrug 含 total_score + 6 个 DimensionScore（验证：单测检查 keys）
- [ ] C11: safety < threshold → excluded=True 且该药品不在 top-K 中（验证：给定 safety=0.1, threshold=0.2 → excluded）
- [ ] C12: 权重自动归一化：输入权重 [1,1,1,1,1,1] → 计算时每个 w = 1/6（验证：单测检查归一化后 Σw = 1.0）

## 确定性验证

- [ ] C13: 相同输入两次调用 score_all → 两次结果 total_score 完全一致（验证：单测 assert score1 == score2）
- [ ] C14: scoring_engine 函数体内无 random、无 time、无 IO 调用（验证：grep -E "random|time\.|async" 在 engine.py 中无匹配）

## 三层权重管理

- [ ] C15: StrategyValidator 拒绝 safety < 0.35 的 safety_first 配置（验证：单测 assert not validator.validate(weights, 'safety_first')[0]）
- [ ] C16: WeightConfigRepository.get_active() 返回 DB 中 is_active=true 的配置（验证：mock DB → 返回正确版本号）
- [ ] C17: TTL 缓存：60 秒内两次 get_active → 只查一次 DB（验证：mock DB 的 call_count == 1）
- [ ] C18: A/B 分桶：同一 session_id 两次调用 → 返回同一版本（验证：单测 assert v1.version == v2.version）

## 可解释性

- [ ] C19: ScoredDrug.dimensions 的每个元素包含 feature_name + weight + feature_value + contribution + evidence_reasons（验证：单测检查 DimensionScore 5 个字段非空）
- [ ] C20: evidence_reasons 可追溯到具体的 Evidence 规则描述（验证：给定 AllergyCheck → reason 包含"过敏"关键词）

## 性能

- [ ] C21: 12 个药品 × 6 个维度的 score_all 耗时 < 15ms（验证：单测 time.perf_counter() 差值 < 0.015）

## 集成

- [ ] C22: ScoringPipeline.run() 串联 EvidenceEngine → ScoringEngine → 返回 ScoringResult（验证：单测 pipeline 返回含 3 个 ScoredDrug 的排序结果）
- [ ] C23: recommend_node 替换后 E2E-1 流程正常走通（验证：验收测试 E2E-1 通过）
- [ ] C24: recommend_node 替换后 E2E-2 安全阻断仍正常工作（验证：验收测试 E2E-2 通过）
- [ ] C25: 降级逻辑：DB 不可用时 pipeline 不崩溃，返回字母序兜底排序（验证：mock DB 抛异常 → pipeline 返回非空结果）

## 编译与测试

- [ ] C26: `python -c "from app.scorer import ScoringPipeline, ScoredDrug, ScoringResult"` 无 ImportError
- [ ] C27: `pytest tests/unit/ -v` 全部通过，覆盖率 ≥ 95%（验证：pytest --cov=app/scorer）
- [ ] C28: `pytest tests/integration/ -v` 全部通过

## 端到端场景

- [ ] E2E-4: 确定性排序验证：同一 session 发送相同症状两次 → 两次推荐药品列表顺序一致（验证：两次 POST /chat → 对比推荐的药品名列表）
- [ ] E2E-5: 安全排除验证：创建对布洛芬过敏的 session → 推荐列表中不出现布洛芬（验证：推荐回复中无"布洛芬"）
- [ ] E2E-6: 年龄适配验证：8 岁儿童感冒发热 → 推荐药品优先有儿童用法的药品（验证：推荐列表第一个药有 usage_child 字段非空）
