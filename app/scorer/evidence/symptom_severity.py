"""
Evidence：发热症状 + 退热成分匹配加成。

当用户有发热症状时，如果药品的有效成分中包含退热成分（布洛芬、
对乙酰氨基酚等），则给 symptom_match 维度额外加成。

这是一条"补充证据"— 它不替换 SymptomKeywordMatch，而是通过 max 合并
机制，在 SymptomKeywordMatch 的基础上提供可能的更高分值。

合并策略：max — 与 SymptomKeywordMatch 通过取最大值合并

示例：
  用户体温 38.5°C，药品含"对乙酰氨基酚" → value=0.85（高加成）
  用户体温 38.5°C，药品不含退热成分    → value=0.5 （弱加成）
  用户无发热                          → value=0.0 （不参与，不影响其他证据）
"""

from app.scorer.evidence.base import BaseEvidence
from app.scorer.schemas import EvidenceResult


# 常见的退热有效成分（中英文对照）
ANTIPYRETIC_INGREDIENTS = {
    "布洛芬", "ibuprofen",
    "对乙酰氨基酚", "acetaminophen", "paracetamol",
    "金刚烷胺", "amantadine",
    "阿司匹林", "aspirin",
}


class SymptomSeverityMatch(BaseEvidence):
    """发热症状与药品退热成分的匹配加成。

    注意：value=0.0 时不影响 max 合并结果（因为 SymptomKeywordMatch
    至少给 0.4）。只有当它给出 >0.4 的分值时才会"胜出"并被采用。
    """

    @property
    def feature_name(self) -> str:
        return "symptom_match"

    @property
    def description(self) -> str:
        return "发热症状与药品退热成分的匹配加成"

    def evaluate(self, slots: dict, drug) -> EvidenceResult:
        """检测用户是否有发热，药品是否含退热成分。

        发热判定（任一满足）：
          1. symptoms 中症状名包含"发热/发烧/高热/低热/体温"
          2. temperature ≥ 37.3°C

        退热成分判定：drug.active_ingredients 与 ANTIPYRETIC_INGREDIENTS 交集
        """
        # ── 检测发热 ──
        has_fever = False
        symptoms = slots.get("symptoms", [])
        for s in symptoms:
            name = s.get("name", "") if isinstance(s, dict) else str(s)
            if any(kw in name for kw in ("发热", "发烧", "高热", "低热", "体温")):
                has_fever = True
                break

        temp = slots.get("temperature")
        if temp is not None and isinstance(temp, (int, float)) and temp >= 37.3:
            has_fever = True

        if not has_fever:
            # 无发热 → 本证据不参与（value=0.0, max 合并时不会胜出）
            return EvidenceResult(
                feature_name=self.feature_name,
                value=0.0,
                reason="用户无发热症状，无需退热成分加成",
                merge_strategy="max",
            )

        # ── 检测药品是否含退热成分 ──
        ingredients = drug.active_ingredients or []
        antipyretic_found = []
        for ing in ingredients:
            if ing in ANTIPYRETIC_INGREDIENTS:
                antipyretic_found.append(ing)

        if antipyretic_found:
            # 有退热成分 → 高加成（通常 > SymptomKeywordMatch 的匹配分）
            return EvidenceResult(
                feature_name=self.feature_name,
                value=0.85,
                reason=f"用户有发热症状，药品含退热成分[{', '.join(antipyretic_found)}]，高度匹配",
                merge_strategy="max",
            )
        else:
            # 有发热但药品不是退热类 → 中等加成
            return EvidenceResult(
                feature_name=self.feature_name,
                value=0.5,
                reason=f"用户有发热症状，但药品主要成分[{', '.join(ingredients)}]非退热类",
                merge_strategy="max",
            )
