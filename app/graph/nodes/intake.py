"""Intake node — preprocess incoming user message."""

from app.graph.state import ConversationState


async def intake_node(state: ConversationState) -> dict:
    """Preprocess the incoming message and prepare state for routing.

    Extracts the last user message from state.messages and updates phase.
    """
    return {
        "phase": "intake",
        "node_events": [{"node": "intake", "status": "ok"}],
    }
