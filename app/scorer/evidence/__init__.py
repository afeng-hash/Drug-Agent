"""
证据规则注册表 — 8 条内置证据规则。

每条规则对应一个特征维度，独立评估 (slots, drug) 的某个方面。

规则清单：
  GraphRelevanceScore   → symptom_match       (max 合并，Neo4j图谱首选)
  SymptomKeywordMatch   → symptom_match       (max 合并，ILIKE降级)
  SymptomSeverityMatch  → symptom_match       (max 合并，发热加成)
  ContraindicationCheck → safety              (min 合并)
  AllergyCheck          → safety              (min 合并)
  AgeSuitability        → age_suitability     (min 合并)
  IngredientCoverage    → ingredient_coverage (max 合并)
  OtcSafetyLevel        → otc_safety_level    (set 合并)

合并策略决定了多条证据对同一特征的影响方式：
  - max：取最大值（匹配类——任一命中就算好）
  - min：取最小值（安全类——任一风险就否决）
  - set：直接覆盖（确定性属性）
  - avg：取平均值（等权综合）
"""

from app.scorer.evidence.symptom_keyword import SymptomKeywordMatch
from app.scorer.evidence.symptom_severity import SymptomSeverityMatch
from app.scorer.evidence.contraindication import ContraindicationCheck
from app.scorer.evidence.allergy import AllergyCheck
from app.scorer.evidence.age_suitability import AgeSuitability
from app.scorer.evidence.ingredient_coverage import IngredientCoverage, OtcSafetyLevel
from app.scorer.evidence.graph_relevance import GraphRelevanceScore

__all__ = [
    "AllergyCheck",
    "AgeSuitability",
    "ContraindicationCheck",
    "GraphRelevanceScore",
    "IngredientCoverage",
    "OtcSafetyLevel",
    "SymptomKeywordMatch",
    "SymptomSeverityMatch",
]
