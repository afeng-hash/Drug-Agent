"""
ToolRegistry — 工具注册中心。

管理 ReactAgent 可用的工具定义和 executor。负责：
  1. 注册工具（定义 + 执行函数）
  2. 输出 OpenAI function calling 格式的 tool definitions
  3. 执行工具调用并捕获异常为 ToolResult
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from app.agent.react.schemas import ToolDefinition, ToolResult

logger = logging.getLogger(__name__)


class ToolRegistry:
    """工具注册中心。

    所有 ReactAgent 工具必须通过此 registry 注册。
    工具分为两部分：
      - definition: ToolDefinition（name, description, parameters JSON Schema, capability）
      - executor:   异步可调用对象（async function），接收关键字参数，返回数据

    使用方式：
        registry = ToolRegistry()

        registry.register(
            ToolDefinition(
                name="search_drug",
                description="搜索药品...",
                parameters={"type": "object", "properties": {...}},
            ),
            my_search_drug_func,
        )

        # 获取 OpenAI tool 格式定义（传给 LLM）
        tools = registry.get_definitions()

        # 执行工具
        result = await registry.execute("search_drug", {"query": "布洛芬"})
    """

    def __init__(self):
        self._definitions: dict[str, ToolDefinition] = {}
        self._executors: dict[str, Callable[..., Any]] = {}

    # ── 注册 ────────────────────────────────────────────

    def register(
        self,
        definition: ToolDefinition,
        executor: Callable[..., Any],
    ) -> None:
        """注册一个工具。

        Args:
            definition: 工具定义（name, description, parameters, capability）
            executor:   工具执行函数（async callable）。签名应匹配 definition.parameters

        Raises:
            ValueError: 工具名已注册
        """
        name = definition.name
        if name in self._definitions:
            raise ValueError(f"Tool '{name}' is already registered")
        self._definitions[name] = definition
        self._executors[name] = executor
        logger.debug("Registered tool: %s (capability=%s)", name, definition.capability)

    # ── 定义获取 ─────────────────────────────────────────

    def get_definitions(self) -> list[dict]:
        """获取所有工具的 OpenAI function calling 格式定义。

        Returns:
            OpenAI tools 参数格式的列表，可直接传给 chat.completions.create(tools=...)
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": d.name,
                    "description": d.description,
                    "parameters": d.parameters,
                },
            }
            for d in self._definitions.values()
        ]

    def get_definition(self, name: str) -> ToolDefinition | None:
        """根据名称获取单个工具定义。

        Args:
            name: 工具名

        Returns:
            ToolDefinition 或 None（未注册）
        """
        return self._definitions.get(name)

    # ── 执行器 ───────────────────────────────────────────

    def get_executor(self, name: str) -> Callable[..., Any] | None:
        """获取工具执行函数。

        Args:
            name: 工具名

        Returns:
            async callable 或 None（未注册）
        """
        return self._executors.get(name)

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        """执行工具调用并捕获异常为 ToolResult。

        工具执行失败不会抛出异常，而是返回 ToolResult(success=False, error=...)。

        Args:
            tool_name: 要执行的工具名
            arguments: 工具参数（关键字参数 dict）

        Returns:
            ToolResult — success=True 时 data 包含工具返回数据，
                         success=False 时 error 包含错误信息
        """
        executor = self._executors.get(tool_name)
        if executor is None:
            return ToolResult(
                tool_name=tool_name,
                success=False,
                error=f"Tool '{tool_name}' not found in registry",
            )

        try:
            result = executor(**arguments)
            # 支持同步和异步 executor
            if asyncio.iscoroutine(result):
                result = await result
            return ToolResult(
                tool_name=tool_name,
                success=True,
                data=result,
            )
        except Exception as e:
            logger.warning("Tool '%s' execution failed: %s", tool_name, e)
            return ToolResult(
                tool_name=tool_name,
                success=False,
                error=str(e),
            )

    async def execute_all(
        self,
        tool_calls: list[dict],
    ) -> list[ToolResult]:
        """并行执行多个工具调用。

        Args:
            tool_calls: LLM 返回的 tool_calls 列表。
                        每个元素包含 id, function.name, function.arguments

        Returns:
            ToolResult 列表，顺序与输入一致
        """
        if not tool_calls:
            return []

        async def _exec_one(tc: dict) -> ToolResult:
            func_info = tc.get("function", tc)
            name = func_info.get("name", "")
            args_str = func_info.get("arguments", "{}")

            import json
            try:
                arguments = json.loads(args_str) if isinstance(args_str, str) else args_str
            except json.JSONDecodeError:
                arguments = {}

            return await self.execute(name, arguments)

        return await asyncio.gather(*[_exec_one(tc) for tc in tool_calls])

    # ── 查询 ─────────────────────────────────────────────

    @property
    def tool_count(self) -> int:
        """已注册工具数量。"""
        return len(self._definitions)

    @property
    def tool_names(self) -> list[str]:
        """已注册工具名称列表。"""
        return list(self._definitions.keys())
