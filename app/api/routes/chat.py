"""Chat endpoint — SSE streaming of Graph execution events."""

import json
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import ChatRequest
from app.db.database import get_db
from app.db.repositories.session import SessionRepository
from app.graph.state import initial_state

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])


@router.post("/{session_id}")
async def chat(
    session_id: str,
    body: ChatRequest,
    request: Request,
) -> StreamingResponse:
    """Process a chat message and stream the response via SSE.

    Each user message triggers one full Graph run.
    Events are streamed as they happen via LangGraph astream_events.
    """
    app_state = request.app.state
    settings = app_state.settings

    # Validate session + load history + save user message
    db_gen = get_db()
    db: AsyncSession = await anext(db_gen)
    try:
        session_repo = SessionRepository(db, expire_minutes=settings.session_expire_minutes)
        session = await session_repo.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        if session.status != "active":
            raise HTTPException(status_code=400, detail=f"Session is {session.status}")

        # Save user message first
        await session_repo.add_message(
            session_id=session_id,
            role="user",
            content=body.message,
        )

        # Eagerly load messages before closing DB session
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload
        from app.db.models import Session as SessionModel
        stmt = (
            select(SessionModel)
            .options(selectinload(SessionModel.messages))
            .where(SessionModel.session_id == session_id)
        )
        result = await db.execute(stmt)
        session_with_msgs = result.scalar_one_or_none()

        messages_history = [
            {"role": m.role, "content": m.content}
            for m in (session_with_msgs.messages if session_with_msgs else [])
        ]
    finally:
        await db.close()

    state = initial_state(session_id=session_id, messages=messages_history)
    graph = app_state.graph

    async def event_generator():
        start_time = time.time()
        final_state = None
        total_tokens = 0

        try:
            async for event in graph.astream_events(state, version="v2"):
                event_type = event.get("event", "")

                if event_type == "on_chain_start":
                    # Node started
                    node_name = event.get("name", "")
                    if node_name in (
                        "intake", "dispatcher", "consult", "safety_check",
                        "recommend", "explain", "inventory", "end",
                    ):
                        yield _sse("node", {"node": node_name, "status": "started"})

                elif event_type == "on_chain_end":
                    # Node ended — extract state updates
                    node_name = event.get("name", "")
                    output = event.get("data", {}).get("output", {})

                    if node_name == "dispatcher" and isinstance(output, dict):
                        route = output.get("dispatcher_result", {}).get("route", "")
                        yield _sse("node", {"node": "dispatcher", "route": route})

                    if isinstance(output, dict) and output.get("response"):
                        response_text = output["response"]
                        # Yield as token events for streaming feel
                        # (Split into chunks to simulate streaming)
                        chunk_size = 10
                        for i in range(0, len(response_text), chunk_size):
                            chunk = response_text[i:i + chunk_size]
                            yield _sse("token", {"content": chunk})
                            total_tokens += 1

                    # Safety check results
                    safety = (output if isinstance(output, dict) else {}).get("safety_result")
                    if safety:
                        yield _sse("safety", {
                            "verdict": safety.get("verdict"),
                            "triggered_rules": safety.get("triggered_rules"),
                        })

                    # Recommendation results
                    recs = (output if isinstance(output, dict) else {}).get("recommendations")
                    if recs:
                        yield _sse("data", {
                            "phase": "recommending",
                            "recommendations": recs,
                        })

                elif event_type == "on_chat_model_stream":
                    # LLM streaming — forward tokens
                    chunk = event.get("data", {}).get("chunk", {})
                    if hasattr(chunk, "choices") and chunk.choices:
                        delta = chunk.choices[0].delta
                        if hasattr(delta, "content") and delta.content:
                            yield _sse("token", {"content": delta.content})
                            total_tokens += 1

            # Done
            elapsed = round(time.time() - start_time, 2)
            yield _sse("done", {
                "session_id": session_id,
                "elapsed_seconds": elapsed,
                "usage": {"tokens": total_tokens},
            })

        except Exception as e:
            yield _sse("error", {
                "code": "INTERNAL_ERROR",
                "message": str(e),
            })

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _sse(event: str, data: dict) -> str:
    """Format a Server-Sent Event."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
