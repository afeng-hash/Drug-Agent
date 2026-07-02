"""Factory function to register all safety rules."""

from app.rules.definitions.r1_high_fever import R1_HighFever
from app.rules.definitions.r2_infant_fever import R2_InfantFever
from app.rules.definitions.r3_pregnant_fever import R3_PregnantFever
from app.rules.definitions.r4_emergency_signs import R4_EmergencySigns
from app.rules.definitions.r5_severe_allergy import R5_SevereAllergy
from app.rules.definitions.r6_drug_allergy import R6_DrugAllergy
from app.rules.definitions.r7_child_aspirin import R7_ChildAspirin
from app.rules.engine import RuleEngine


def register_all_rules(engine: RuleEngine) -> None:
    """Register all 7 MVP safety rules into the engine."""
    engine.register(R1_HighFever())
    engine.register(R2_InfantFever())
    engine.register(R3_PregnantFever())
    engine.register(R4_EmergencySigns())
    engine.register(R5_SevereAllergy())
    engine.register(R6_DrugAllergy())
    engine.register(R7_ChildAspirin())
