"""
SearchManualTool — 药品问答工具（向量检索）。

在药品说明书中语义检索与用户问题最相关的片段。
后端：Milvus（DrugManualRetriever）。
能力：drug_qa。

这是药品针对性问答的**首选工具**——向量搜索能精准定位说明书中的相关段落，
比 DB 的结构化字段更适合回答"布洛芬有什么副作用""孕妇能吃布洛芬吗"等问题。
"""

from app.agent.react.schemas import ToolDefinition
from app.agent.react.tools.base import BaseTool


class SearchManualTool(BaseTool):
    """药品问答 — 向量语义检索说明书。

    用于：
      - 针对性药品问答（副作用、禁忌、用法用量等）
      - get_drug_detail 失败时的 Milvus 兜底
      - 需要从说明书原文中查找细节信息

    这是大多数药品问答场景的**首选工具**。
    """

    fallback_tools = ["get_drug_detail"]
    capabilities = ["drug_qa"]

    def __init__(self, retriever):
        self._retriever = retriever

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="search_manual",
            description=(
                "在药品说明书中语义检索与用户问题最相关的片段。"
                "这是回答针对性药品问题的**首选工具**（如副作用、禁忌、"
                "用法用量、孕妇/儿童用药等）。"
                "适用场景：用户问'XX药有什么副作用''XX药孕妇能吃吗'"
                "'XX药怎么吃'等具体问题。"
                "注意：如需药品的完整结构化档案（所有字段），请使用 get_drug_detail。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "drug_name": {
                        "type": "string",
                        "description": "药品名称（通用名）",
                    },
                    "question": {
                        "type": "string",
                        "description": "用户关心的问题，如'副作用''孕妇能用吗''用法用量'",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "返回片段数量，默认 5",
                    },
                },
                "required": ["drug_name", "question"],
            },
        )

    async def execute(
        self, drug_name: str, question: str, top_k: int = 5
    ) -> list[dict] | dict:
        """检索药品说明书。"""
        if not self._retriever:
            return {"error": "说明书检索服务不可用"}
        try:
            chunks = await self._retriever.retrieve_multi(
                drug_name, question=question, top_k=top_k
            )
            return [
                {
                    "content": c.content if hasattr(c, "content") else str(c),
                    "source": getattr(c, "source", "") if hasattr(c, "source") else "",
                }
                for c in chunks
            ]
        except Exception as e:
            return {"error": f"说明书检索失败：{str(e)}"}
