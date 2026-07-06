"""
ReactAgent 数据模型。

定义 ReactAgent 的 Tool 定义、调用、结果、步骤、运行结果和 WorkingMemory。
这些模型不依赖 LangGraph，不 import ConversationState，保证 ReactAgent 的独立性。
"""

from typing import Any

from pydantic import BaseModel, Field


class ToolDefinition(BaseModel):
    """工具定义 — 同时用于 OpenAI function calling 和内部注册。

    parameters 字段直接作为 OpenAI tool 的 function.parameters 传入，
    使用 JSON Schema 格式描述参数约束。
    """

    name: str
    """工具唯一标识，对应 OpenAI function.name"""

    description: str
    """工具描述，LLM 据此判断何时调用"""

    parameters: dict = Field(default_factory=dict)
    """JSON Schema 格式的参数定义。如：
    {"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}
    """

    capability: str = "read"
    """权限级别。"read" 表示只读，"write" 表示可写。
    ReactAgent 的工具一律为 "read"。
    """


class ToolCall(BaseModel):
    """LLM 发起的一次工具调用。"""

    id: str
    """OpenAI 生成的工具调用唯一 ID"""

    tool_name: str
    """被调用的工具名"""

    arguments: dict = Field(default_factory=dict)
    """LLM 生成的调用参数"""


class ToolResult(BaseModel):
    """单次工具调用的结果。"""

    tool_name: str
    """被调用的工具名"""

    success: bool
    """执行是否成功"""

    data: Any = None
    """成功时返回的数据"""

    error: str | None = None
    """失败时的错误信息"""


class AgentStep(BaseModel):
    """Agent ReAct 循环中的一步。"""

    iteration: int
    """第几轮迭代（从 1 开始）"""

    thought: str | None = None
    """LLM 在决定调用工具前的思考（如有）"""

    tool_calls: list[ToolCall] = Field(default_factory=list)
    """本轮 LLM 请求的工具调用列表"""

    tool_results: list[ToolResult] = Field(default_factory=list)
    """本轮工具调用的执行结果"""


class AgentResult(BaseModel):
    """ReactAgent 运行完成的输出。"""

    final_response: str
    """最终回复文本（用户可见）"""

    steps: list[AgentStep] = Field(default_factory=list)
    """完整步骤记录，用于 node_events 日志和审计"""

    total_iterations: int = 0
    """总迭代次数"""

    total_time_ms: float = 0.0
    """总耗时（毫秒）"""


class WorkingMemory(BaseModel):
    """Agent 工作记忆 — 单次 ReactAgent 调用内的短期记忆。

    用于缓存工具结果避免重复调用，以及记录 agent 自己的中间发现。
    """

    intermediate_findings: dict[str, Any] = Field(default_factory=dict)
    """tool_name → summarized result。缓存工具调用结果"""

    context_notes: list[str] = Field(default_factory=list)
    """agent 自己的备注，用于跨轮推理"""
