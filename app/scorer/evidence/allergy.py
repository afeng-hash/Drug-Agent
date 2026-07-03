"""
Evidence：用户过敏史 与 药品有效成分交叉检测。

检查用户自述的药物过敏列表中是否有与该药品有效成分匹配的项。

合并策略：min — 对任何一种成分过敏 = 该药品不安全

匹配逻辑：
  1. 用户 allergies 列表中每一项 ↔ 药品 active_ingredients 列表中每一项
  2. 子串匹配（"布洛芬"出现在"布洛芬"中即命中）
  3. 也检查药品通用名（用户说对"布洛芬"过敏 → 该药品通用名叫"布洛芬"也命中）

示例：
  用户过敏史: ["阿司匹林", "青霉素"]
  药品成分:   ["布洛芬"]  → value=1.0（无交叉，安全）
  药品成分:   ["阿司匹林"] → value=0.0（过敏，排除）
"""

from app.scorer.evidence.base import BaseEvidence
from app.scorer.schemas import EvidenceResult


class AllergyCheck(BaseEvidence):
    """过敏史与药品成分的交叉检测。"""

    @property
    def feature_name(self) -> str:
        return "safety"

    @property
    def description(self) -> str:
        return "用户过敏史与药品有效成分的交叉检测"

    def evaluate(self, slots: dict, drug) -> EvidenceResult:
        allergies = slots.get("allergies", [])
        if not allergies:
            # 无过敏史 → 满分
            return EvidenceResult(
                feature_name=self.feature_name,
                value=1.0,
                reason="用户无已知药物过敏史",
                merge_strategy="min",
            )

        ingredients = [ing.lower() for ing in (drug.active_ingredients or [])]
        drug_name = drug.generic_name.lower()

        for allergy in allergies:
            allergy_lower = str(allergy).lower()

            # 检查 1：过敏原 vs 药品有效成分
            for ing in ingredients:
                if allergy_lower in ing or ing in allergy_lower:
                    return EvidenceResult(
                        feature_name=self.feature_name,
                        value=0.0,  # 完全排除
                        reason=f"用户对{allergy}过敏，该药品含{ing}成分，禁止使用",
                        merge_strategy="min",
                    )

            # 检查 2：过敏原 vs 药品通用名
            if allergy_lower in drug_name or drug_name in allergy_lower:
                return EvidenceResult(
                    feature_name=self.feature_name,
                    value=0.0,
                    reason=f"用户对{allergy}过敏，该药品({drug.generic_name})可能引起过敏反应",
                    merge_strategy="min",
                )

        # 无交叉 → 安全
        return EvidenceResult(
            feature_name=self.feature_name,
            value=1.0,
            reason=f"用户过敏史[{', '.join(str(a) for a in allergies)}]与该药品成分无交叉",
            merge_strategy="min",
        )
