"""
CheckInventoryTool — 药品库存查询工具。

根据药品名称查询库存信息：是否有货、库存数量、价格、规格、厂家、货架位置。
后端：PostgreSQL（DrugRepository + InventoryRepository）。
能力：inventory_check。
"""

from app.agent.react.schemas import ToolDefinition
from app.agent.react.tools.base import BaseTool


class CheckInventoryTool(BaseTool):
    """药品库存查询 — 查询药品是否有货、价格、货架位置。

    用于：
      - 用户询问药品是否有货（"布洛芬还有吗"）
      - 用户询问药品价格（"布洛芬多少钱"）
      - 用户想确认药店是否有某药品（"有没有布洛芬"）
    """

    fallback_tools = ["search_drug"]
    capabilities = ["inventory_check"]

    def __init__(self, drug_repo_factory, inventory_repo_factory):
        self._drug_repo_factory = drug_repo_factory
        self._inventory_repo_factory = inventory_repo_factory

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="check_inventory",
            description=(
                "查询药品库存信息：是否有货、库存数量、价格、规格、厂家、"
                "货架位置。适用场景：用户询问药品是否有货、药品价格、"
                "或想确认药店是否有某药品。注意：需要提供药品通用名（如'布洛芬'），"
                "如果不确定药品名，先用 search_drug 查找。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "drug_name": {
                        "type": "string",
                        "description": "药品通用名（如'布洛芬'、'对乙酰氨基酚'）",
                    },
                },
                "required": ["drug_name"],
            },
        )

    async def execute(self, drug_name: str) -> dict:
        """查询药品库存。

        流程：
          1. 按通用名查找药品 → 获取 drug_id
          2. 按 drug_id 查所有可售库存
          3. 格式化返回

        Args:
            drug_name: 药品通用名

        Returns:
            {
                "drug_name": "布洛芬",
                "found": True/False,
                "total_stock": 总库存数量,
                "items": [{
                    "product_name": "布洛芬缓释胶囊",
                    "manufacturer": "中美天津史克",
                    "specification": "0.3g×24粒",
                    "price": 18.50,
                    "stock_quantity": 25,
                    "shelf_location": "A-3-2",
                    "status": "有货" | "缺货" | "库存紧张"
                }, ...]
            }
        """
        async with self._drug_repo_factory() as drug_repo:
            drug = await drug_repo.find_by_name(drug_name)
            if not drug:
                # 精确匹配失败 → 同会话内模糊搜索，避免额外 DB 连接
                candidates = await drug_repo.search(drug_name, limit=3)
                if candidates:
                    drug = candidates[0]

        if not drug:
            return {
                "drug_name": drug_name,
                "found": False,
                "error": f"未找到药品：{drug_name}",
                "suggestion": "请确认药品名称是否正确，或尝试使用药品通用名查询",
            }

        drug_id = _attr(drug, "id")
        generic_name = _attr(drug, "generic_name", drug_name)

        async with self._inventory_repo_factory() as inv_repo:
            items = await inv_repo.find_by_drug(drug_id)

        if not items:
            return {
                "drug_name": generic_name,
                "drug_id": drug_id,
                "found": True,
                "total_stock": 0,
                "items": [],
                "summary": f"{generic_name} 目前缺货，暂无库存",
            }

        total_stock = sum(item.stock_quantity for item in items)
        formatted_items = []
        for item in items:
            qty = item.stock_quantity
            if qty <= 0:
                status = "缺货"
            elif qty < 10:
                status = f"库存紧张 (仅剩{qty}件)"
            else:
                status = "有货"

            formatted_items.append({
                "product_name": item.product_name,
                "manufacturer": item.manufacturer,
                "specification": item.specification,
                "price": item.price,
                "stock_quantity": qty,
                "shelf_location": item.shelf_location,
                "status": status,
            })

        return {
            "drug_name": generic_name,
            "drug_id": drug_id,
            "found": True,
            "total_stock": total_stock,
            "items": formatted_items,
            "summary": (
                f"{generic_name} 共有 {len(formatted_items)} 个SKU，"
                f"总库存 {total_stock} 件"
            ),
        }


def _attr(obj, name: str, default=None):
    """兼容 ORM 对象和 dict。"""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)
