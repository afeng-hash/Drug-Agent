"""
FindDrugLocationTool — 药品货架位置查询工具。

根据药品名称查询货架位置：在哪个货架、什么规格、什么价格。
后端：PostgreSQL（DrugRepository + InventoryRepository）。
能力：inventory_check。
"""

from app.agent.react.schemas import ToolDefinition
from app.agent.react.tools.base import BaseTool


class FindDrugLocationTool(BaseTool):
    """药品货架位置查询 — 查询药品在药店的货架位置。

    用于：
      - 用户询问药品在哪里（"布洛芬在哪里""退烧药在哪个货架"）
      - 用户想找到药品的具体位置（"帮我找一下布洛芬"）
    """

    fallback_tools = ["search_drug", "check_inventory"]
    capabilities = ["inventory_check"]

    def __init__(self, drug_repo_factory, inventory_repo_factory):
        self._drug_repo_factory = drug_repo_factory
        self._inventory_repo_factory = inventory_repo_factory

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="find_drug_location",
            description=(
                "查询药品在药店中的货架位置。适用场景：用户询问药品在哪里、"
                "在哪个货架、怎么找到某药品。注意：需要提供药品通用名（如'布洛芬'），"
                "如果不确定药品名，先用 search_drug 查找。"
                "如果用户同时关心库存和价格，改用 check_inventory。"
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
        """查询药品货架位置。

        流程：
          1. 按通用名查找药品 → 获取 drug_id
          2. 按 drug_id 查所有可售库存 → 提取货架位置
          3. 格式化返回

        Args:
            drug_name: 药品通用名

        Returns:
            {
                "drug_name": "布洛芬",
                "found": True/False,
                "locations": [{
                    "product_name": "布洛芬缓释胶囊",
                    "specification": "0.3g×24粒",
                    "manufacturer": "中美天津史克",
                    "price": 18.50,
                    "shelf_location": "A-3-2",
                    "in_stock": True/False
                }, ...]
            }
        """
        async with self._drug_repo_factory() as drug_repo:
            drug = await drug_repo.find_by_name(drug_name)
            if not drug:
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
                "locations": [],
                "summary": f"{generic_name} 目前暂无库存，无法确定货架位置",
            }

        locations = []
        for item in items:
            locations.append({
                "product_name": item.product_name,
                "specification": item.specification,
                "manufacturer": item.manufacturer,
                "price": item.price,
                "shelf_location": item.shelf_location or "未标注",
                "in_stock": item.stock_quantity > 0,
            })

        return {
            "drug_name": generic_name,
            "drug_id": drug_id,
            "found": True,
            "locations": locations,
            "summary": (
                f"{generic_name} 共有 {len(locations)} 个SKU在售"
            ),
        }


def _attr(obj, name: str, default=None):
    """兼容 ORM 对象和 dict。"""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)
