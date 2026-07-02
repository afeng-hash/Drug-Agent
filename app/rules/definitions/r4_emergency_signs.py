"""R4: Emergency signs (breathing difficulty, chest pain, confusion) → BLOCK."""

from app.rules.base import RuleResult, SafetyRule

EMERGENCY_KEYWORDS = [
    "呼吸困难", "胸痛", "意识模糊", "昏迷",
    "抽搐", "剧烈头痛", "吐血", "便血",
]


class R4_EmergencySigns(SafetyRule):
    rule_id = "R4"
    description = "出现呼吸困难、胸痛、意识模糊等急症信号 → 阻断推荐"

    def evaluate(self, slots: dict) -> RuleResult:
        other_symptoms = slots.get("other_symptoms", [])
        if not other_symptoms:
            return RuleResult()

        other_text = " ".join(other_symptoms).lower()
        for keyword in EMERGENCY_KEYWORDS:
            if keyword in other_text:
                return RuleResult(
                    triggered=True,
                    action="BLOCK",
                    reason=f"您提到了「{keyword}」，这可能是紧急情况的信号，请立即前往医院急诊科就诊，不要自行用药。",
                )
        return RuleResult()
