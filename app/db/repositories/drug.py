"""Drug repository — queries for the drugs table."""

from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Drug


class DrugRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def find_by_name(self, generic_name: str) -> Drug | None:
        """Find a drug by exact generic name."""
        stmt = select(Drug).where(Drug.generic_name == generic_name)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def find_by_symptoms(
        self, symptoms: list[str], category: str = "感冒退烧"
    ) -> list[Drug]:
        """Find drugs whose indication_summary matches any of the symptoms."""
        conditions = []
        for symptom in symptoms:
            conditions.append(Drug.indication_summary.ilike(f"%{symptom}%"))
        stmt = (
            select(Drug)
            .where(
                Drug.category == category,
                or_(*conditions) if conditions else True,
            )
            .order_by(Drug.generic_name)
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def list_all(self, category: str | None = None) -> list[Drug]:
        """List all drugs, optionally filtered by category."""
        stmt = select(Drug)
        if category:
            stmt = stmt.where(Drug.category == category)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def find_by_ids(self, drug_ids: list[int]) -> list[Drug]:
        """Find drugs by their IDs."""
        stmt = select(Drug).where(Drug.id.in_(drug_ids))
        result = await self.db.execute(stmt)
        return list(result.scalars().all())
