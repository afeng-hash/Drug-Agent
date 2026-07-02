"""R6: Drug allergy → FILTER out matching drugs."""

from app.rules.base import RuleResult, SafetyRule

# Map allergen name → drug generic names to exclude
ALLERGY_DRUG_MAP = {
    "布洛芬": ["布洛芬"],
    "ibuprofen": ["布洛芬"],
    "阿司匹林": ["阿司匹林"],
    "aspirin": ["阿司匹林"],
    "对乙酰氨基酚": ["对乙酰氨基酚"],
    "扑热息痛": ["对乙酰氨基酚"],
    "paracetamol": ["对乙酰氨基酚"],
    "头孢": ["头孢氨苄", "头孢拉定"],
    "青霉素": [],
    "磺胺": [],
}


class R6_DrugAllergy(SafetyRule):
    rule_id = "R6"
    description = "用户自述药物过敏 → 排除对应药物"

    def evaluate(self, slots: dict) -> RuleResult:
        allergies = slots.get("allergies", [])
        if not allergies:
            return RuleResult()

        excluded = []
        reasons = []
        for allergy in allergies:
            allergy_lower = allergy.lower().strip()
            for key, drugs in ALLERGY_DRUG_MAP.items():
                if allergy_lower in key or key in allergy_lower:
                    excluded.extend(drugs)
                    reasons.append(f"用户对{allergy}过敏，排除{'、'.join(drugs)}")

        if excluded:
            return RuleResult(
                triggered=True,
                action="FILTER",
                reason="；".join(reasons),
                excluded_drugs=list(set(excluded)),
            )
        return RuleResult()
