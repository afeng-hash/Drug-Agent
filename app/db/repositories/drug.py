"""
Drug repository — drugs 表的查询封装。

提供药品的增删改查方法，所有方法都接收 AsyncSession 作为构造函数参数。
Repository 本身不管理 session 生命周期，由调用方（Graph 节点/API 路由）负责。
"""

from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Drug


class DrugRepository:
    """药品信息查询器。

    使用方式：
        async with get_db() as db:
            repo = DrugRepository(db)
            drug = await repo.find_by_name("布洛芬")
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    async def find_by_name(self, generic_name: str) -> Drug | None:
        """按通用名精确查找药品。

        如 find_by_name("布洛芬") → Drug 对象 或 None。

        Args:
            generic_name: 药品通用名（必须在 drugs 表中完全匹配）

        Returns:
            匹配的 Drug 对象，不存在则返回 None
        """
        stmt = select(Drug).where(Drug.generic_name == generic_name)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def find_by_symptoms(
        self, symptoms: list[str], category: str = "感冒退烧"
    ) -> list[Drug]:
        """按症状关键词模糊查找药品。

        对每个症状词做 indication_summary 的 ILIKE 模糊匹配，
        多个症状词之间是 OR 关系（匹配到任一个就行）。

        如 find_by_symptoms(["头痛", "发烧"]) →
            返回所有适应症中包含"头痛"或"发烧"的药品

        Args:
            symptoms: 症状名称列表，如 ["头痛", "发烧", "流鼻涕"]
            category: 药品类别过滤，默认 "感冒退烧"

        Returns:
            匹配的药品列表（按通用名排序）
        """
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
        """列出所有药品（可选按类别过滤）。

        Args:
            category: 药品类别，传 None 则不过滤

        Returns:
            全部符合条件的药品列表
        """
        stmt = select(Drug)
        if category:
            stmt = stmt.where(Drug.category == category)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def find_by_ids(self, drug_ids: list[int]) -> list[Drug]:
        """按 ID 列表批量查询药品。

        如 find_by_ids([1, 2, 3]) → 返回 ID 为 1, 2, 3 的三个药品

        Args:
            drug_ids: 药品 ID 列表

        Returns:
            匹配的药品列表
        """
        stmt = select(Drug).where(Drug.id.in_(drug_ids))
        result = await self.db.execute(stmt)
        return list(result.scalars().all())
