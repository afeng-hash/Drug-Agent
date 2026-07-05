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
        symptoms = slots.get("symptoms", [])
        if not symptoms:
            return RuleResult()

        # 从统一 symptoms 列表中提取纯文本名称
        symptom_names: list[str] = []
        for s in symptoms:
            if isinstance(s, dict):
                name = s.get("name", "")
                if name:
                    symptom_names.append(name)
            elif isinstance(s, str):
                symptom_names.append(s)

        if not symptom_names:
            return RuleResult()

        symptom_text = "".join(symptom_names)
        for keyword in ALLERGY_KEYWORDS:
            if keyword in symptom_text:
                return RuleResult(
                    triggered=True,
                    action="BLOCK",
                    reason="您描述的症状可能是严重过敏反应，需要立即就医处理，口服抗过敏药可能不足以应对。",
                )
        return RuleResult()
