"""
ReactAgent 模块 — 工具驱动的 LLM Agent。

独立于 LangGraph，可单独测试和复用。
处理药品查询、对比、相互作用、闲聊等所有非"症状求药"场景。
"""

from app.agent.react.agent import ReactAgent
from app.agent.react.memory import WorkingMemory as WorkingMemoryRuntime
from app.agent.react.schemas import (
    AgentResult,
    AgentStep,
    ToolCall,
    ToolDefinition,
    ToolResult,
    WorkingMemory,
)
from app.agent.react.tools import ToolRegistry

__all__ = [
    "ReactAgent",
    "ToolDefinition",
    "ToolRegistry",
    "AgentResult",
    "AgentStep",
    "ToolCall",
    "ToolResult",
    "WorkingMemory",
    "WorkingMemoryRuntime",
]
