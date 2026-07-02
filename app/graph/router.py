"""Conditional edge functions for the LangGraph state machine."""

from app.graph.state import ConversationState


def route_after_dispatcher(state: ConversationState) -> str:
    """After dispatcher: route based on dispatcher_result.route."""
    route = state.get("dispatcher_result", {}).get("route", "consult")
    valid_routes = {"consult", "explain", "recommend", "end"}
    return route if route in valid_routes else "consult"


def route_after_consult(state: ConversationState) -> str:
    """After consult: if done → safety_check; otherwise END (wait for user)."""
    next_action = state.get("consult_next_action", "ask")
    if next_action == "done":
        return "safety_check"
    return "end"


def route_after_safety(state: ConversationState) -> str:
    """After safety check: BLOCK → end; PASS/FILTER → recommend."""
    safety_result = state.get("safety_result") or {}
    verdict = safety_result.get("verdict", "PASS")
    if verdict == "BLOCK":
        return "end"
    return "recommend"
