"""Rule engine base types — abstract rule definition."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class RuleResult:
    """Output of evaluating a single safety rule."""

    triggered: bool = False
    action: str = "NONE"  # "BLOCK" | "FILTER" | "NONE"
    reason: str = ""
    excluded_drugs: list[str] = field(default_factory=list)


class SafetyRule(ABC):
    """Abstract base class for all safety rules."""

    rule_id: str = ""
    description: str = ""

    @abstractmethod
    def evaluate(self, slots: dict) -> RuleResult:
        """Evaluate this rule against the current consult slots.

        Args:
            slots: The current ConsultSlots as a dict.

        Returns:
            RuleResult with triggered, action, reason, and optional excluded_drugs.
        """
        ...
