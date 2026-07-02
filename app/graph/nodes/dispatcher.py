"""Dispatcher node — the conversation dispatcher/routing controller."""

import json

from pydantic import BaseModel, Field

from app.agent.prompts import DISPATCHER_PROMPT
from app.graph.state import ConversationState, normalize_messages
from app.llm.client import LLMClient


class DispatcherDecision(BaseModel):
    """Structured output from the dispatcher."""
    route: str = Field(
        description="Target node: consult | explain | recommend | end"
    )
    intent: str = Field(
        description="User intent: describe_symptom | ask_drug | switch_drug | switch_symptom | give_up | other"
    )
    params: dict = Field(
        default_factory=dict,
        description="Extra params: {drug_name, reset_slots, filter_cheaper}",
    )


async def dispatcher_node(state: ConversationState, llm_client: LLMClient) -> dict:
    """Analyze context + user message, decide route and intent.

    Args:
        state: Current ConversationState.
        llm_client: Injected LLM client.

    Returns:
        State updates including dispatcher_result, previous_phase, phase.
    """
    messages = normalize_messages(state.get("messages", []))
    if not messages:
        return _fallback_route()

    # Get the latest user message
    latest_user = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            latest_user = m.get("content", "")
            break

    if not latest_user.strip():
        return _fallback_route()

    # Build context for the dispatcher
    slots = state.get("consult_slots", {})
    current_phase = state.get("phase", "intake")
    previous_phase = state.get("previous_phase")

    context = {
        "current_phase": current_phase,
        "previous_phase": previous_phase,
        "collected_slots_summary": _summarize_slots(slots),
        "user_message": latest_user,
    }

    prompt_messages = [
        {"role": "system", "content": DISPATCHER_PROMPT},
        {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
    ]

    try:
        decision = await llm_client.generate_structured(
            messages=prompt_messages,
            schema=DispatcherDecision,
            temperature=0.2,
            max_tokens=256,
        )
    except Exception:
        return _fallback_route()

    new_previous_phase = previous_phase
    # If jumping to explain from consulting, record previous_phase to return later
    if decision.route == "explain" and current_phase in ("consulting", "intake"):
        new_previous_phase = current_phase

    # If giving up or ending, clear previous_phase
    if decision.route == "end" and decision.intent == "give_up":
        new_previous_phase = None

    return {
        "dispatcher_result": {
            "route": decision.route,
            "intent": decision.intent,
            "params": decision.params,
        },
        "previous_phase": new_previous_phase,
        "phase": current_phase,
        "node_events": [{
            "node": "dispatcher",
            "route": decision.route,
            "intent": decision.intent,
        }],
    }


def _fallback_route() -> dict:
    """Default route when dispatcher can't determine intent."""
    return {
        "dispatcher_result": {
            "route": "consult",
            "intent": "fallback",
            "params": {},
        },
        "node_events": [{"node": "dispatcher", "route": "consult", "intent": "fallback"}],
    }


def _summarize_slots(slots: dict) -> str:
    """Create a human-readable summary of current slots."""
    if not slots:
        return "暂无已收集的症状信息"

    parts = []
    symptoms = slots.get("symptoms", [])
    if symptoms:
        names = [s.get("name", s) if isinstance(s, dict) else str(s) for s in symptoms]
        parts.append(f"症状: {', '.join(names)}")

    temp = slots.get("temperature")
    if temp is not None:
        parts.append(f"体温: {temp}°C")

    days = slots.get("duration_days")
    if days is not None:
        parts.append(f"持续: {days}天")

    pop = slots.get("special_population")
    if pop:
        parts.append(f"特殊人群: {pop}")

    age = slots.get("age")
    if age is not None:
        parts.append(f"年龄: {age}岁")

    allergies = slots.get("allergies", [])
    if allergies:
        parts.append(f"过敏史: {', '.join(allergies)}")

    return "；".join(parts) if parts else "暂无已收集的症状信息"
