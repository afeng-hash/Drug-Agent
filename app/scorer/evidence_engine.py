"""
证据引擎（EvidenceEngine）— 管理证据规则、执行评估、合并为特征向量。

这是评分管线的第二步（第一步是加载权重配置）。EvidenceEngine
注册了 7 条证据规则，对每个候选药品逐条执行，然后将结果按合并策略
聚合成一个 FeatureVector（dict[str, float]）。

核心设计：
  - 每条规则独立评估 (slots, drug)，输出 EvidenceResult
  - 同一个 feature_name 可能被多条规则影响
  - 合并策略（merge_strategy）决定了当多条规则影响同一特征时如何处理：
    - 'max' → 取最大值（症状匹配类：任一命中即高分）
    - 'min' → 取最小值（安全类：任一风险即否决）
    - 'avg' → 取平均值（多条证据等权重）
    - 'set' → 直接覆盖（确定性属性，无竞争）

默认特征值（DEFAULT_FEATURES）是"没有任何证据"时的初始状态：
  - symptom_match: 0.0       ← 还没匹配，从零开始
  - safety: 1.0              ← 假设安全，直到证据证明有风险
  - age_suitability: 0.5     ← 中性（不知道年龄段）
  - otc_safety_level: 0.7    ← 假设甲类（偏保守）
  - ingredient_coverage: 0.0 ← 还没计算覆盖度
  - evidence_quality: 0.5    ← 中性（预留维度）
"""

from app.scorer.evidence.base import BaseEvidence
from app.scorer.schemas import EvidenceResult


# 默认特征值：在没有任何证据规则被应用之前的初始状态
DEFAULT_FEATURES: dict[str, float] = {
    "symptom_match": 0.0,       # 症状匹配：从零开始，证据给出正向分值
    "safety": 1.0,              # 安全性：假设安全，证据发现风险后降低
    "age_suitability": 0.5,     # 年龄适用性：中性（尚不知道年龄）
    "otc_safety_level": 0.7,    # OTC 安全等级：假设甲类（偏保守默认）
    "ingredient_coverage": 0.0, # 成分覆盖度：从零开始计算
    "evidence_quality": 0.5,    # 证据质量：中性（预留，当前无证据规则填充此维度）
}


class EvidenceEngine:
    """管理证据规则注册与执行，将多条 EvidenceResult 合并为 FeatureVector。

    使用方式：
        engine = EvidenceEngine()                       # 用默认初始值
        engine.register(SymptomKeywordMatch())           # 注册规则
        engine.register(AllergyCheck())
        ...
        features, details = engine.evaluate_with_detail(slots, drug)
        # features = {"symptom_match": 0.85, "safety": 1.0, ...}

    注册顺序不影响最终结果 — 最终值由合并策略决定。
    """

    def __init__(self, defaults: dict[str, float] | None = None):
        """初始化证据引擎。

        Args:
            defaults: 可选的默认特征值。不传则使用 DEFAULT_FEATURES。
                      传入空 dict 也是合法的（所有维度从 0 开始）。
        """
        self._rules: list[BaseEvidence] = []
        self._defaults = defaults or dict(DEFAULT_FEATURES)

    @property
    def rule_count(self) -> int:
        """已注册的证据规则数量。用于调试和健康检查。"""
        return len(self._rules)

    def register(self, evidence: BaseEvidence) -> None:
        """注册一条证据规则。

        注册顺序无关紧要 — 最终的特征值由合并策略（max/min/avg/set）
        决定，而不是注册顺序。可以随时增删规则而不影响确定性。

        Args:
            evidence: BaseEvidence 子类实例
        """
        self._rules.append(evidence)

    def evaluate(self, slots: dict, drug) -> dict[str, float]:
        """执行所有证据规则，返回最终的 FeatureVector。

        这是 evaluate_with_detail() 的简化版 — 只返回特征值，
        丢弃证据详情。适合只需最终分数的场景。

        Args:
            slots: consult_slots 字典
            drug:  Drug ORM 实例

        Returns:
            FeatureVector: dict[str, float]，如 {"symptom_match": 0.85, "safety": 1.0}
        """
        features, _ = self.evaluate_with_detail(slots, drug)
        return features

    def evaluate_with_detail(
        self, slots: dict, drug
    ) -> tuple[dict[str, float], list[EvidenceResult]]:
        """执行所有证据规则，返回特征向量和逐条结果。

        这是完整的评估方法，返回：
          - FeatureVector（给 ScoringEngine 算分用）
          - EvidenceResult 列表（给审计/解释用）

        处理流程：
          1. 从默认值初始化 FeatureVector
          2. 依次执行每条规则
          3. 按 merge_strategy 合并到 FeatureVector
          4. 'avg' 策略在所有规则执行完后统一计算

        Args:
            slots: consult_slots 字典
            drug:  Drug ORM 实例

        Returns:
            (FeatureVector dict, EvidenceResult 列表) 元组
        """
        # 从默认值开始（后续会被证据规则修改）
        features = dict(self._defaults)
        all_results: list[EvidenceResult] = []

        # 'avg' 策略需要先收集所有值，最后统一计算平均值
        avg_buffers: dict[str, list[float]] = {}

        for rule in self._rules:
            result = rule.evaluate(slots, drug)
            all_results.append(result)

            key = result.feature_name
            strategy = result.merge_strategy

            if strategy == "set":
                # 直接覆盖 — 适用于确定性属性（如 OTC 分类）
                features[key] = result.value

            elif strategy == "max":
                # 取最大值 — 适用于匹配类（多条证据，任一命中即高分）
                if key not in features or result.value > features.get(key, 0.0):
                    features[key] = result.value

            elif strategy == "min":
                # 取最小值 — 适用于安全类（多条证据，任一风险即否决）
                if key not in features or result.value < features.get(key, 1.0):
                    features[key] = result.value

            elif strategy == "avg":
                # 收集后统一计算平均值
                if key not in avg_buffers:
                    avg_buffers[key] = []
                avg_buffers[key].append(result.value)

        # 计算 'avg' 合并：对每个 key 的所有值取平均
        for key, values in avg_buffers.items():
            features[key] = sum(values) / len(values)

        return features, all_results
