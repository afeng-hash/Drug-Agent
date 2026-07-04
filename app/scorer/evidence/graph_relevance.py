"""
证据（Evidence）：基于图数据库的“症状 → 药物”相关性评分。

本模块从 Drug ORM 对象的一个临时属性中，读取 Neo4j 图评分。
该评分由 `DrugGraphRepository.find_candidates_by_symptoms` 方法计算得出，
计算公式为：覆盖率 × 精确度（coverage × precision）。

- 当 Neo4j 可用时：该评分将替代 `SymptomKeywordMatch` 中粗糙的 `ILIKE` 文本模糊匹配，
  提供一个更精细的连续型评分（范围 0-1）。
- 当 Neo4j 不可用时：本模块返回 0.0，此时 `SymptomKeywordMatch`（ILIKE 匹配）
  将通过“取最大值（max）”的合并策略自动接管评分计算。
"""

from app.scorer.evidence.base import BaseEvidence
from app.scorer.schemas import EvidenceResult


class GraphRelevanceScore(BaseEvidence):
    """将 Neo4j 图谱 (Coverage × Precision) 分数作为 symptom_match 特征值。

    当 Neo4j 可用时，用图谱连续分（0-1）替换 SymptomKeywordMatch 的
    粗粒度 ILIKE 文本匹配（仅 4 个离散档位）。
    Neo4j 不可用时返回 0.0，让 ILIKE 降级自动生效。

    merge_strategy="max" 确保图谱分和 ILIKE 分竞争，高分者胜出。
    """

    @property
    def feature_name(self) -> str:
        return "symptom_match"

    @property
    def description(self) -> str:
        return "Neo4j图谱症状-药品相关性评分（Coverage × Precision）"

    def evaluate(self, slots: dict, drug) -> EvidenceResult:
        """从 Drug ORM 临时属性读取图谱分数。

        Drug._graph_score 由 recommend_node._fetch_candidates() 在
        获取候选药品时设置。None 表示 Neo4j 不可用或未设置。
        """
        score = getattr(drug, '_graph_score', None)
        if score is not None:
            # 将分数钳制（Clamp）在 [0, 1] 区间内。
            # 虽然当匹配到多个症状时，调整后的总分（adjusted score）可能会超过 1.0，
            # 但按照特征值（feature value）的规范约定，该值必须保持在 0 到 1 之间。
            value = min(max(float(score), 0.0), 1.0)
            return EvidenceResult(
                feature_name=self.feature_name,
                value=value,
                reason=f"图谱相关性: coverage×precision={score:.3f}",
                merge_strategy="max",
            )

        # Neo4j unavailable → return 0.0 so SymptomKeywordMatch (ILIKE)
        # takes over via max merge strategy.
        return EvidenceResult(
            feature_name=self.feature_name,
            value=0.0,
            reason="图谱不可用，降级为文本匹配",
            merge_strategy="max",
        )
