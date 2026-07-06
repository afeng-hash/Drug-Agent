"""
GetDrugDetailTool — 药品完整档案工具。

获取药品的结构化信息：适应症、用法用量、不良反应、禁忌、相互作用等。
后端：MySQL（DrugRepository），不含 RAG 增强（RAG 由 search_manual 提供）。
能力：drug_profile。
"""

from app.agent.react.schemas import ToolDefinition
from app.agent.react.tools.base import BaseTool


class GetDrugDetailTool(BaseTool):
    """药品完整档案 — 结构化药品信息查询。

    用于：
      - 用户需要药品的完整信息（"布洛芬的详细信息"）
      - search_manual 失败时的 DB 兜底
    """

    fallback_tools = ["search_manual"]
    capabilities = ["drug_profile"]

    def __init__(self, drug_repo_factory):
        self._drug_repo_factory = drug_repo_factory

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="get_drug_detail",
            description=(
                "获取药品的完整结构化信息：适应症、用法用量、不良反应、"
                "禁忌、药物相互作用、注意事项等。适用场景：用户需要药品的"
                "系统性介绍或完整档案时使用。注意：如需针对特定问题的精准"
                "检索（如'布洛芬孕妇能吃吗'），优先使用 search_manual。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "drug_name": {
                        "type": "string",
                        "description": "药品通用名（如'布洛芬'）",
                    },
                },
                "required": ["drug_name"],
            },
        )

    async def execute(self, drug_name: str) -> dict:
        """获取药品完整信息。"""
        async with self._drug_repo_factory() as repo:
            drug = await repo.find_by_name(drug_name)
            if not drug:
                return {"error": f"未找到药品：{drug_name}"}

            return _drug_to_dict(drug, drug_name)


def _drug_to_dict(drug, drug_name: str) -> dict:
    """将 Drug ORM 对象转为 dict。兼容 dict 输入。"""
    if isinstance(drug, dict):
        return drug
    return {
        "drug_id": getattr(drug, "drug_id", None),
        "generic_name": getattr(drug, "generic_name", drug_name),
        "trade_names": getattr(drug, "trade_names", ""),
        "category": getattr(drug, "category", ""),
        "indications": getattr(drug, "indications", ""),
        "usage_dosage": getattr(drug, "usage_dosage", ""),
        "adverse_reactions": getattr(drug, "adverse_reactions", ""),
        "contraindications": getattr(drug, "contraindications", ""),
        "interactions": getattr(drug, "interactions", ""),
        "precautions": getattr(drug, "precautions", ""),
    }
