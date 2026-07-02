"""
Inventory repository — inventory 表的查询封装。

提供库存查询方法。只查询可售（is_available=True）的商品。
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Inventory


class InventoryRepository:
    """库存查询器。"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def find_by_drug(self, drug_id: int) -> list[Inventory]:
        """查询某个药品的所有可售库存。

        结果按价格升序（最便宜的排在前面）。

        Args:
            drug_id: 药品 ID（drugs 表主键）

        Returns:
            该药品的所有可售库存记录列表
        """
        stmt = (
            select(Inventory)
            .where(Inventory.drug_id == drug_id, Inventory.is_available == True)
            .order_by(Inventory.price)  # 便宜优先
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def find_by_drugs(self, drug_ids: list[int]) -> list[Inventory]:
        """批量查询多个药品的可售库存。

        SQL 示例：
            SELECT * FROM inventory
            WHERE drug_id IN (1, 2, 3) AND is_available = true
            ORDER BY drug_id, price

        Args:
            drug_ids: 药品 ID 列表

        Returns:
            所有匹配的库存记录（先按 drug_id 分组，组内按价格升序）
        """
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
