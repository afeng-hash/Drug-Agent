"""R2: Infant (<3 months) with fever → BLOCK."""

from app.rules.base import RuleResult, SafetyRule


class R2_InfantFever(SafetyRule):
    rule_id = "R2"
    description = "年龄 < 3 个月且发热 → 阻断推荐"

    def evaluate(self, slots: dict) -> RuleResult:
        age = slots.get("age")
        temp = slots.get("temperature")
        # Age is in years; 3 months ≈ 0.25 years
        if (
            age is not None
            and age < 0.25
            and temp is not None
            and temp > 37.3
        ):
            return RuleResult(
                triggered=True,
                action="BLOCK",
                reason="3个月以下的婴儿发热属于紧急情况，请不要自行用药，立即前往儿科急诊就医。",
            )
        return RuleResult()
