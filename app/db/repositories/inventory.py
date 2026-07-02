"""Inventory repository — queries for the inventory table."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Inventory


class InventoryRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def find_by_drug(self, drug_id: int) -> list[Inventory]:
        """Find all inventory items for a given drug."""
        stmt = (
            select(Inventory)
            .where(Inventory.drug_id == drug_id, Inventory.is_available == True)
            .order_by(Inventory.price)
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def find_by_drugs(self, drug_ids: list[int]) -> list[Inventory]:
        """Find inventory items for multiple drugs."""
        stmt = (
            select(Inventory)
            .where(
                Inventory.drug_id.in_(drug_ids),
                Inventory.is_available == True,
            )
            .order_by(Inventory.drug_id, Inventory.price)
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())
