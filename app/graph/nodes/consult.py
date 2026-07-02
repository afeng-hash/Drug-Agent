"""Consult node — ReAct symptom gathering via consult agent."""

from app.agent.consult_agent import run_consult
from app.graph.state import ConversationState
from app.llm.client import LLMClient


async def consult_node(
    state: ConversationState,
    llm_client: LLMClient,
    max_rounds: int = 6,
) -> dict:
    """Run one round of consult: update slots, decide next action.

    Returns state updates including consult_slots, consult_next_action,
    consult_summary, response, phase.
    """
    messages = state.get("messages", [])
    slots = state.get("consult_slots", {})
    dispatcher_params = state.get("dispatcher_result", {}).get("params", {})
    reset_slots = dispatcher_params.get("reset_slots", False)

    # If user switched symptoms, reset slots
    if reset_slots:
        slots = {
            "symptoms": [],
            "temperature": None,
            "duration_days": None,
            "medications_taken": [],
            "special_population": None,
            "age": None,
            "chronic_conditions": [],
            "allergies": [],
            "other_symptoms": [],
        }

    result = await run_consult(
        llm_client=llm_client,
        messages=messages,
        current_slots=slots,
        max_rounds=max_rounds,
    )

    return {
        "consult_slots": result.updated_slots,
        "consult_next_action": result.next_action,
        "consult_summary": result.summary,
        "response": result.response,
        "phase": "consulting" if result.next_action == "ask" else "consulting",
        "node_events": [{
            "node": "consult",
            "next_action": result.next_action,
            "summary": result.summary,
        }],
    }
