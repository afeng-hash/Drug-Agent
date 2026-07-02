"""End node — persist session state and finalize the turn."""

from app.db.repositories.safety_log import SafetyLogRepository
from app.db.repositories.session import SessionRepository


async def end_node(
    state: dict,
    session_repo: SessionRepository,
    safety_log_repo: SafetyLogRepository | None = None,
) -> dict:
    """Finalize the turn: persist messages, log safety results.

    Args:
        state: Current ConversationState.
        session_repo: Injected SessionRepository.
        safety_log_repo: Injected SafetyLogRepository.

    Returns:
        State updates with phase="ended".
    """
    session_id = state.get("session_id", "")
    response = state.get("response", "")
    dispatcher_result = state.get("dispatcher_result", {})
    intent = dispatcher_result.get("intent")

    # Save AI response to session history
    if session_id and response:
        try:
            await session_repo.add_message(
                session_id=session_id,
                role="assistant",
                content=response,
                intent=intent,
                metadata={"phase": state.get("phase")},
            )
        except Exception:
            pass

    # Log safety check result
    safety_result = state.get("safety_result")
    if safety_result and safety_log_repo and session_id:
        try:
            session_obj = await session_repo.get(session_id)
            if session_obj:
                await safety_log_repo.log(
                    session_id=session_obj.id,
                    verdict=safety_result.get("verdict", "PASS"),
                    triggered_rules=safety_result.get("triggered_rules", []),
                    input_slots=state.get("consult_slots", {}),
                )
        except Exception:
            pass

    return {
        "phase": "ended",
        "node_events": [{"node": "end", "status": "ok"}],
    }
