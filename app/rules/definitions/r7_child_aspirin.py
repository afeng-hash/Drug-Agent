"""R7: Child (<12 years) + aspirin-containing drugs → FILTER."""

from app.rules.base import RuleResult, SafetyRule

ASPIRIN_DRUGS = ["阿司匹林", "复方阿司匹林"]


class R7_ChildAspirin(SafetyRule):
    rule_id = "R7"
    description = "儿童（<12 岁）且药品含阿司匹林 → 排除该药物"

    def evaluate(self, slots: dict) -> RuleResult:
        age = slots.get("age")
        if age is not None and age < 12:
            return RuleResult(
                triggered=True,
                action="FILTER",
                reason="儿童使用阿司匹林可能引发瑞氏综合征（Reye's syndrome），已自动排除含阿司匹林的药物。",
                excluded_drugs=ASPIRIN_DRUGS,
            )
        return RuleResult()
