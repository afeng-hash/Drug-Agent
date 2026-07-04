"""
证据规则注册表 — 4 条内置证据规则。

每条规则对应一个特征维度，独立评估 (slots, drug) 的某个方面。

规则清单：
  GraphRelevanceScore → symptom_match        (set 合并，Neo4j图谱 adjusted score)
  SymptomFocusRatio   → symptom_focus_ratio  (set 合并，集合覆盖比)
  AgeSuitability      → age_suitability      (set 合并，年龄段适用性)
  OtcSafetyLevel      → otc_safety_level     (set 合并，OTC分类等级)

已移除的规则（KG硬过滤替代 / 功能冗余）：
  ContraindicationCheck → 由 KG _filter_by_kg_contraindications 硬过滤替代
  AllergyCheck          → 由 KG _filter_by_kg_contraindications 硬过滤替代
  SymptomKeywordMatch   → ILIKE 降级不再需要（KG 始终可用）
  SymptomSeverityMatch  → KG TREATS 关系已覆盖发热维度
  IngredientCoverage    → 与 symptom_match 功能重复

合并策略：
  - set：直接覆盖（确定性属性，无竞争）
"""

from app.scorer.evidence.age_suitability import AgeSuitability
from app.scorer.evidence.ingredient_coverage import OtcSafetyLevel
from app.scorer.evidence.graph_relevance import GraphRelevanceScore
from app.scorer.evidence.symptom_focus import SymptomFocusRatio

__all__ = [
    "AgeSuitability",
    "GraphRelevanceScore",
    "OtcSafetyLevel",
    "SymptomFocusRatio",
]
