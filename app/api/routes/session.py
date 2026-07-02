"""Session management endpoints — create, query sessions."""

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.schemas import SessionDetailResponse, SessionResponse
from app.db.database import get_db
from app.db.models import Session as SessionModel
from app.db.repositories.session import SessionRepository

router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])


@router.post("", response_model=SessionResponse, status_code=201)
async def create_session(request: Request) -> SessionResponse:
    """Create a new anonymous session."""
    settings = request.app.state.settings
    db_gen = get_db()
    db: AsyncSession = await anext(db_gen)
    try:
        repo = SessionRepository(db, expire_minutes=settings.session_expire_minutes)
        session = await repo.create()
        return SessionResponse(
            session_id=session.session_id,
            status=session.status,
            created_at=session.created_at.isoformat(),
            expires_at=session.expires_at.isoformat(),
        )
    finally:
        await db.close()


@router.get("/{session_id}", response_model=SessionDetailResponse)
async def get_session(
    session_id: str,
    request: Request,
) -> SessionDetailResponse:
    """Get session status and message history."""
    settings = request.app.state.settings
    db_gen = get_db()
    db: AsyncSession = await anext(db_gen)
    try:
        repo = SessionRepository(db, expire_minutes=settings.session_expire_minutes)
        session = await repo.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")

        # Eagerly load messages to avoid lazy-load issues
        stmt = (
            select(SessionModel)
            .options(selectinload(SessionModel.messages))
            .where(SessionModel.session_id == session_id)
        )
        result = await db.execute(stmt)
        session_with_msgs = result.scalar_one_or_none()

        messages = []
        if session_with_msgs and session_with_msgs.messages:
            messages = [
                {
                    "role": m.role,
                    "content": m.content,
                    "timestamp": m.created_at.isoformat(),
                }
                for m in session_with_msgs.messages
            ]

        return SessionDetailResponse(
            session_id=session.session_id,
            status=session.status,
            created_at=session.created_at.isoformat(),
            expires_at=session.expires_at.isoformat(),
            messages=messages,
        )
    finally:
        await db.close()
