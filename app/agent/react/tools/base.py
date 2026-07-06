"""
BaseTool — ReactAgent 工具抽象基类。

每个工具封装为一个 BaseTool 子类，内聚：
  - definition:   工具定义（name, description, parameters）
  - execute():    工具执行逻辑
  - fallback_tools: 失败时 LLM 可以尝试的替代工具名
  - capabilities:  能力标签（用于自动生成 prompt 中的容错矩阵）

加一个新工具 = 新建一个文件，builder.py 里加一行。
"""

from abc import ABC, abstractmethod
from typing import Any

from app.agent.react.schemas import ToolDefinition


class BaseTool(ABC):
    """ReactAgent 工具基类。

    子类必须实现：
      - definition (property) → ToolDefinition
      - execute(**kwargs) → Any

    子类可选覆盖：
      - fallback_tools: list[str] — 失败时的替代工具
      - capabilities: list[str]  — 能力标签
    """

    fallback_tools: list[str] = []
    """本工具执行失败时，LLM 可以尝试的替代工具名列表。
    例如 SearchDrugTool 失败 → 可尝试 get_drug_detail 或 search_manual。
    """

    capabilities: list[str] = []
    """能力标签，用于自动生成 prompt 中的工具容错矩阵。
    例如 ["drug_discovery"]、["drug_qa"]、["state_access"]。
    同一能力的工具互为替代。
    """

    @property
    @abstractmethod
    def definition(self) -> ToolDefinition:
        """工具定义 — 传给 LLM 的 name/description/parameters。"""
        ...

    @abstractmethod
    async def execute(self, **kwargs) -> Any:
        """执行工具逻辑。

        子类实现具体的工具行为。异常会被 ToolRegistry.execute()
        统一捕获为 ToolResult(success=False)。
        """
        ...
