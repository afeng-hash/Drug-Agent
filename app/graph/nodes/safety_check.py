"""SafetyCheck node — run deterministic safety rules before recommending."""

from app.graph.state import ConversationState
from app.rules.engine import RuleEngine


async def safety_check_node(
    state: ConversationState,
    rule_engine: RuleEngine,
) -> dict:
    """Run the rule engine against current consult slots.

    Returns state updates including safety_result.
    """
    slots = state.get("consult_slots", {})
    # Get candidate drug names from recommendations (may be empty at this point)
    candidate_drugs = [
        r.get("generic_name", "") for r in state.get("recommendations", [])
    ]

    result = rule_engine.check(slots, candidate_drugs)

    safety_result = {
        "verdict": result.verdict,
        "triggered_rules": result.triggered_rules,
        "excluded_drugs": result.excluded_drugs,
        "message": result.message,
    }

    response = state.get("response", "")
    if result.verdict == "BLOCK":
        response = result.message

    return {
        "safety_result": safety_result,
        "response": response,
        "node_events": [{
            "node": "safety_check",
            "verdict": result.verdict,
            "triggered_rules": [r["rule_id"] for r in result.triggered_rules],
        }],
    }
