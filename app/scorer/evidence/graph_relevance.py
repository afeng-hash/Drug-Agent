"""
证据（Evidence）：基于图数据库的“症状 → 药物”相关性评分。

本模块从 Drug ORM 对象的一个瞬态属性（transient attribute）中读取 Neo4j 图评分。
该评分由 `DrugGraphRepository.find_candidates_by_symptoms` 方法计算得出，
计算公式为：覆盖率 × 精确度（经过特异性惩罚调整）。

注：本模块是“症状匹配（symptom_match）”特征维度的唯一数据来源。
"""

from app.scorer.evidence.base import BaseEvidence
from app.scorer.schemas import EvidenceResult


class GraphRelevanceScore(BaseEvidence):
    """使用 Neo4j 图评分（覆盖率 × 精确度）作为“症状匹配（symptom_match）”的特征值。

     基于包含 IS_A 层级关系的图遍历算法，并结合特异性惩罚（specificity penalty），
     提供一个连续型的评分（范围 0-1）。

     merge_strategy="set" -- 作为“症状匹配”特征的唯一数据来源，不参与竞争合并。
    """

    @property
    def feature_name(self) -> str:
        return "symptom_match"

    @property
    def description(self) -> str:
        return "Neo4j graph symptom-drug relevance (Coverage x Precision)"

    def evaluate(self, slots: dict, drug) -> EvidenceResult:
        """从 Drug ORM 对象的瞬态属性（transient attribute）中读取图评分。

         注：`Drug._graph_score` 属性是在 `recommend_node._fetch_candidates()`
             方法拉取候选药物时被赋值的。
             如果该值为 None，则表示知识图谱（KG）数据当前不可用。
        """
        score = getattr(drug, '_graph_score', None)
        if score is not None:
            value = min(max(float(score), 0.0), 1.0)
            return EvidenceResult(
                feature_name=self.feature_name,
                value=value,
                reason=f"Graph relevance: coverage x precision = {score:.3f}",
                merge_strategy="set",
            )

        # KG data unavailable → return neutral value (geometric mean: 0.5 is
        # close to neutral; returning 0.0 would crush the score via ln(≈0)).
        return EvidenceResult(
            feature_name=self.feature_name,
            value=0.5,
            reason="KG data unavailable, default neutral",
            merge_strategy="set",
        )
