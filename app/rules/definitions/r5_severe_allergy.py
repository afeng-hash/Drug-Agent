"""R5: Severe allergic reaction (rash, anaphylaxis) → BLOCK."""

from app.rules.base import RuleResult, SafetyRule

ALLERGY_KEYWORDS = [
    "全身皮疹", "全身过敏", "过敏性休克",
    "喉头水肿", "呼吸困难伴皮疹",
]


class R5_SevereAllergy(SafetyRule):
    rule_id = "R5"
    description = "全身皮疹/严重过敏反应 → 阻断推荐"

    def evaluate(self, slots: dict) -> RuleResult:
        other_symptoms = slots.get("other_symptoms", [])
        if not other_symptoms:
            return RuleResult()

        other_text = "".join(other_symptoms)
        for keyword in ALLERGY_KEYWORDS:
            if keyword in other_text:
                return RuleResult(
                    triggered=True,
                    action="BLOCK",
                    reason="您描述的症状可能是严重过敏反应，需要立即就医处理，口服抗过敏药可能不足以应对。",
                )
        return RuleResult()
