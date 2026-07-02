"""Rule Engine — deterministic safety check, two-phase execution."""

from app.rules.base import RuleResult, SafetyRule


class SafetyResult:
    """Aggregated result from the rule engine."""

    def __init__(self):
        self.verdict: str = "PASS"  # "PASS" | "BLOCK" | "FILTER"
        self.triggered_rules: list[dict] = []
        self.excluded_drugs: list[str] = []
        self.message: str = ""


class RuleEngine:
    """Deterministic safety rule engine.

    Executes rules in two phases:
      1. BLOCK rules — any trigger short-circuits and stops.
      2. FILTER rules — aggregates exclusion lists.
    """

    def __init__(self):
        self._rules: list[SafetyRule] = []

    def register(self, rule: SafetyRule) -> None:
        """Register a safety rule."""
        self._rules.append(rule)

    def check(
        self, slots: dict, drug_names: list[str] | None = None
    ) -> SafetyResult:
        """Run all registered rules against the given slots.

        Args:
            slots: ConsultSlots as a dict (symptoms, temperature, etc.).
            drug_names: List of candidate drug generic names (for FILTER rules).

        Returns:
            SafetyResult with verdict, triggered_rules, excluded_drugs, message.
        """
        result = SafetyResult()
        drug_names = drug_names or []

        # ── Phase 1: BLOCK rules (short-circuit on first trigger) ──
        for rule in self._rules:
            rule_result = rule.evaluate(slots)
            if rule_result.triggered and rule_result.action == "BLOCK":
                result.verdict = "BLOCK"
                result.triggered_rules.append({
                    "rule_id": rule.rule_id,
                    "action": "BLOCK",
                    "reason": rule_result.reason,
                })
                result.message = self._build_block_message(result.triggered_rules)
                return result  # short-circuit

        # ── Phase 2: FILTER rules (aggregate exclusions) ──
        for rule in self._rules:
            rule_result = rule.evaluate(slots)
            if rule_result.triggered and rule_result.action == "FILTER":
                result.triggered_rules.append({
                    "rule_id": rule.rule_id,
                    "action": "FILTER",
                    "reason": rule_result.reason,
                })
                for drug in rule_result.excluded_drugs:
                    if drug in drug_names:
                        result.excluded_drugs.append(drug)

        if result.excluded_drugs:
            result.verdict = "FILTER"

        return result

    @staticmethod
    def _build_block_message(triggered: list[dict]) -> str:
        reasons = [r["reason"] for r in triggered]
        return (
            "⚠️ 系统检测到以下危险信号，建议您立即就医，不要自行用药：\n"
            + "\n".join(f"  • {r}" for r in reasons)
            + "\n\n本系统仅为辅助参考，不能替代专业医疗诊断。请尽快前往医院就诊。"
        )
