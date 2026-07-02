"""Safety log repository — audit trail for safety rule checks."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SafetyLog


class SafetyLogRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def log(
        self,
        session_id: int,
        verdict: str,
        triggered_rules: list[dict],
        input_slots: dict,
    ) -> SafetyLog:
        """Record a safety check result."""
        log_entry = SafetyLog(
            session_id=session_id,
            verdict=verdict,
            triggered_rules=triggered_rules,
            input_slots=input_slots,
        )
        self.db.add(log_entry)
        await self.db.commit()
        await self.db.refresh(log_entry)
        return log_entry
