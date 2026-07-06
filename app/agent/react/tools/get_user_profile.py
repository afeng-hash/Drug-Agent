"""
GetUserProfileTool — 获取用户个人信息。

读取问诊过程中收集的用户档案（年龄、过敏史、慢性病、特殊人群状态）。
后端：_StateProxy（内存）。
能力：state_access。
"""

from app.agent.react.schemas import ToolDefinition
from app.agent.react.tools.base import BaseTool


class GetUserProfileTool(BaseTool):
    """状态读取 — 获取用户个人信息。

    用于：
      - 个性化药品建议（如"布洛芬对胃有刺激，您有胃溃疡史需要注意"）
      - 回答涉及禁忌症的问题时结合用户档案
    """

    fallback_tools = []
    capabilities = ["state_access"]

    def __init__(self, state_proxy):
        self._state_proxy = state_proxy

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="get_user_profile",
            description=(
                "获取用户个人信息（年龄、过敏史、慢性病、特殊人群等）。"
                "用于个性化药品回答——根据用户的年龄、过敏史等给出针对性建议。"
            ),
            parameters={"type": "object", "properties": {}},
        )

    async def execute(self) -> dict:
        """返回用户档案。"""
        return dict(self._state_proxy.user_profile)
