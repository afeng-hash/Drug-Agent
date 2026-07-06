"""
SearchWebTool — 联网搜索兜底工具。

当本地工具（search_manual、get_drug_detail）返回空或不充分时，
作为最后一级数据源进行联网搜索。后端：Bing Web Search API。
能力：web_search。
"""

from app.agent.react.schemas import ToolDefinition
from app.agent.react.tools.base import BaseTool
from app.search.service import WebSearchService


class SearchWebTool(BaseTool):
    """联网搜索 — 第三级数据源兜底。

    仅在本地工具（DB/Milvus）返回空数据或不充分信息时使用。
    返回结果带来源 URL，LLM 必须在回复中标注网络来源。
    """

    fallback_tools = []
    capabilities = ["web_search"]

    def __init__(self, web_search_service: WebSearchService):
        self._service = web_search_service

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="search_web",
            description=(
                "联网搜索药品信息。⚠️ 这是最后一级数据源——"
                "仅在本地工具（search_manual、get_drug_detail）"
                "返回空结果或信息不充分时才使用。"
                "搜索 query 应包含药品名称 + 用户问题的关键词。"
                "返回结果包含来源 URL，你必须在回复中标注网络来源。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "搜索关键词，应包含药品名称和用户问题的核心词。"
                            '如"布洛芬 孕妇 安全性 说明书"'
                        ),
                    },
                    "num_results": {
                        "type": "integer",
                        "description": "返回结果数量，默认 5",
                    },
                },
                "required": ["query"],
            },
        )

    async def execute(self, query: str, num_results: int = 5) -> dict:
        """执行联网搜索。

        Returns:
            dict 含 found 标记、results 列表和 warning 免责声明。
            即使服务不可用也返回结构化 dict（不抛异常）。
        """
        response = await self._service.search(query, num_results=num_results)

        return {
            "found": len(response.results) > 0,
            "query": response.query,
            "results": [
                {
                    "title": r.title,
                    "snippet": r.snippet,
                    "url": r.url,
                    "source": "web",
                }
                for r in response.results
            ],
            "total_estimated": response.total_estimated,
            "source": "web",
            "warning": response.warning,
        }
