"""Session repository — CRUD for anonymous sessions."""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Message, Session


class SessionRepository:
    def __init__(self, db: AsyncSession, expire_minutes: int = 30):
        self.db = db
        self.expire_minutes = expire_minutes

    async def create(self) -> Session:
        """Create a new anonymous session."""
        now = datetime.now(timezone.utc)
        session = Session(
            session_id=str(uuid.uuid4()),
            status="active",
            expires_at=now + timedelta(minutes=self.expire_minutes),
            created_at=now,
            updated_at=now,
        )
        self.db.add(session)
        await self.db.commit()
        await self.db.refresh(session)
        return session

    async def get(self, session_id: str) -> Session | None:
        """Get session by session_id. Auto-closes expired sessions."""
        stmt = select(Session).where(Session.session_id == session_id)
        result = await self.db.execute(stmt)
        session = result.scalar_one_or_none()
        if session is None:
            return None

        # Auto-expire
        now = datetime.now(timezone.utc)
        if session.expires_at < now and session.status == "active":
            session.status = "expired"
            session.updated_at = now
            await self.db.commit()
            await self.db.refresh(session)
        return session

    async def close(self, session_id: str) -> None:
        """Manually close a session."""
        stmt = (
            update(Session)
            .where(Session.session_id == session_id)
            .values(status="closed", updated_at=datetime.now(timezone.utc))
        )
        await self.db.execute(stmt)
        await self.db.commit()

    async def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        intent: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        """Add a message to the session's history."""
        session = await self.get(session_id)
        if session is None:
            raise ValueError(f"Session {session_id} not found")
        message = Message(
            session_id=session.id,
            role=role,
            content=content,
            intent=intent,
            metadata_=metadata,
        )
        self.db.add(message)
        # Touch session updated_at
        session.updated_at = datetime.now(timezone.utc)
        await self.db.commit()
