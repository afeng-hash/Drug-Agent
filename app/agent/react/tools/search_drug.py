"""
SearchDrugTool — 药品发现工具。

按药品名称（通用名/商品名/拼音）模糊搜索，返回匹配的药品列表。
后端：MySQL（DrugRepository）。
能力：drug_discovery。
"""

from app.agent.react.schemas import ToolDefinition
from app.agent.react.tools.base import BaseTool


class SearchDrugTool(BaseTool):
    """药品发现 — 按名称/类别搜索药品。

    用于：
      - 用户提到药品名时定位药品（"布洛芬"、"芬必得"）
      - 用户需要某类药品时发现候选（"有什么退烧药"）
    """

    fallback_tools = ["get_drug_detail", "search_manual"]
    capabilities = ["drug_discovery"]

    def __init__(self, drug_repo_factory):
        self._drug_repo_factory = drug_repo_factory

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="search_drug",
            description=(
                "搜索药品。根据药品名称（通用名/商品名/拼音）模糊搜索，"
                "返回匹配的药品列表。适用场景：用户提到药品名时定位药品、"
                "用户需要某类药品时发现候选。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "药品名称关键词",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返回数量上限，默认 5",
                    },
                },
                "required": ["query"],
            },
        )

    async def execute(self, query: str, limit: int = 5) -> list[dict]:
        """搜索药品。"""
        async with self._drug_repo_factory() as repo:
            results = await repo.search(query, limit=limit)
            return [
                {
                    "drug_id": self._attr(r, "drug_id"),
                    "generic_name": self._attr(r, "generic_name"),
                    "trade_names": self._attr(r, "trade_names"),
                }
                for r in results
            ]

    @staticmethod
    def _attr(obj, name: str, default: str = ""):
        """兼容 ORM 对象和 dict。"""
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)
