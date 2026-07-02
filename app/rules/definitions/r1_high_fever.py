"""
R1: 高热持续多日 → BLOCK（阻断推荐）

规则：体温 ≥ 39°C 且持续 ≥ 3 天
行动：BLOCK — 立即返回就医警告，不推荐任何药品
原因：持续高热可能是严重感染（肺炎、败血症等）的信号，OTC 药品无效且可能延误病情
"""
from app.rules.base import RuleResult, SafetyRule


class R1_HighFever(SafetyRule):
    """高热阻断规则：≥39°C 且持续 ≥3 天。"""

    rule_id = "R1"
    description = "体温 ≥ 39°C 且持续 ≥ 3 天 → 阻断推荐"

    def evaluate(self, slots: dict) -> RuleResult:
        """检查体温和持续时间两个槽位。

        Args:
            slots: consult_slots dict（读取 temperature 和 duration_days）
        """
        temp = slots.get("temperature")
        days = slots.get("duration_days")

        # 两个条件都满足才触发
        if temp is not None and temp >= 39.0 and days is not None and days >= 3:
            return RuleResult(
                triggered=True,
                action="BLOCK",
                reason="您已持续高热（39°C以上）超过3天，可能存在严重感染或其他疾病，建议立即就医检查，不要自行用药。",
            )
        return RuleResult()  # 未触发
