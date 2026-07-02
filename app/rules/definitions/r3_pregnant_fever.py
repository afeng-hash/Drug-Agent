"""R3: Pregnant + fever ≥ 38.5°C → BLOCK."""

from app.rules.base import RuleResult, SafetyRule


class R3_PregnantFever(SafetyRule):
    rule_id = "R3"
    description = "孕妇且体温 ≥ 38.5°C → 阻断推荐"

    def evaluate(self, slots: dict) -> RuleResult:
        population = slots.get("special_population")
        temp = slots.get("temperature")
        if (
            population == "pregnant"
            and temp is not None
            and temp >= 38.5
        ):
            return RuleResult(
                triggered=True,
                action="BLOCK",
                reason="孕妇体温超过38.5°C可能对胎儿造成影响，请立即就医，在医生指导下用药。",
            )
        return RuleResult()
