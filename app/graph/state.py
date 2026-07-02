"""ConversationState — the shared state object passed through all Graph nodes."""

from typing import Annotated, Any, TypedDict

from langgraph.graph.message import add_messages


class ConversationState(TypedDict):
    """Shared state for the entire conversation graph.

    Persists across multiple Graph runs (turns).
    """

    session_id: str
    """Unique session identifier (UUID)."""

    messages: Annotated[list[dict[str, Any]], add_messages]
    """Full conversation history: {role, content, timestamp}."""

    phase: str
    """Current phase: "intake" | "consulting" | "recommending" | "explaining" | "ended"."""

    previous_phase: str | None
    """Phase before a topic switch (used to return after Explain)."""

    consult_slots: dict[str, Any]
    """Structured symptom information (ConsultSlots).
    Keys: symptoms, temperature, duration_days, medications_taken,
          special_population, age, chronic_conditions, allergies, other_symptoms.
    """

    dispatcher_result: dict[str, Any]
    """Result of the Dispatcher node: {route, intent, params}."""

    consult_next_action: str
    """Consult node decision: "ask" | "done"."""

    consult_summary: str
    """Symptom summary generated when consult is done."""

    safety_result: dict[str, Any] | None
    """Safety check result: {verdict, triggered_rules, excluded_drugs, message}."""

    recommendations: list[dict[str, Any]]
    """Recommended drugs: [{drug_id, generic_name, brand_name, match_reason, score}]."""

    response: str
    """The response text to send back to the user this turn."""

    node_events: list[dict[str, Any]]
    """Metadata events emitted by nodes during this run (for SSE streaming)."""


# Map LangChain message types to OpenAI-compatible roles
_LC_ROLE_MAP = {
    "human": "user",
    "user": "user",
    "ai": "assistant",
    "assistant": "assistant",
    "system": "system",
    "tool": "tool",
    "function": "function",
}


def normalize_messages(messages: list) -> list[dict]:
    """Convert LangChain message objects to plain dicts with 'role' and 'content'.

    LangGraph's add_messages reducer converts dict messages to HumanMessage/AIMessage
    objects. This utility normalizes them back to OpenAI-compatible dict format.
    """
    result = []
    for m in messages:
        if isinstance(m, dict):
            role = m.get("role", "user")
            role = _LC_ROLE_MAP.get(role, role)
            result.append({"role": role, "content": str(m.get("content", ""))})
        else:
            lc_type = getattr(m, "type", "unknown")
            role = _LC_ROLE_MAP.get(lc_type, lc_type)
            content = getattr(m, "content", "")
            result.append({"role": role, "content": str(content)})
    return result


def initial_state(session_id: str, messages: list[dict] | None = None) -> ConversationState:
    """Create a fresh ConversationState for a new turn."""
    return ConversationState(
        session_id=session_id,
        messages=messages or [],
        phase="intake",
        previous_phase=None,
        consult_slots={
            "symptoms": [],
            "temperature": None,
            "duration_days": None,
            "medications_taken": [],
            "special_population": None,
            "age": None,
            "chronic_conditions": [],
            "allergies": [],
            "other_symptoms": [],
        },
        dispatcher_result={},
        consult_next_action="ask",
        consult_summary="",
        safety_result=None,
        recommendations=[],
        response="",
        node_events=[],
    )
