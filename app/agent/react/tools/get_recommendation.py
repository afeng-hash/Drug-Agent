"""
GetRecommendationTool — 获取当前推荐列表。

读取系统本轮已推荐的药品列表，用于解析"这个药"等指代。
后端：_StateProxy（内存）。
能力：state_access。
"""

from app.agent.react.schemas import ToolDefinition
from app.agent.react.tools.base import BaseTool


class GetRecommendationTool(BaseTool):
    """状态读取 — 获取系统当前已推荐的药品列表。

    用于：
      - 用户说"这个药的副作用"、"推荐的那个药"等指代
      - ReactAgent 需要知道 workflow 推荐了哪些药
    """

    fallback_tools = []
    capabilities = ["state_access"]

    def __init__(self, state_proxy):
        self._state_proxy = state_proxy

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="get_recommendation",
            description=(
                "获取系统当前已推荐的药品列表。"
                "当用户使用'这个药'、'推荐的药'、'它'等指代词时，"
                "先调用此工具获取推荐列表，再据此解析用户指代的是哪个药。"
            ),
            parameters={"type": "object", "properties": {}},
        )

    async def execute(self) -> list[dict]:
        """返回当前推荐列表。"""
        return list(self._state_proxy.recommendations)
