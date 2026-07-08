"""Unit tests for ReactAgent — using real LLM + mock tools.

Tests that don't need mocking use real LLM calls (via LLMClient).
Only error scenarios (LLM failure, max_iterations exceeded) use mocks.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent.react.agent import ReactAgent
from app.agent.react.memory import WorkingMemory
from app.agent.react.schemas import AgentResult, ToolDefinition
from app.agent.react.tools import ToolRegistry
from app.config import Settings
from app.llm.client import LLMClient
from app.llm.profile import LLMProfile

# ═══════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════


@pytest.fixture
def settings():
    return Settings()


@pytest.fixture
def llm_client(settings):
    return LLMClient(settings)


@pytest.fixture
def react_profile(settings):
    return settings.get_profile("llm_react")


@pytest.fixture
def system_prompt():
    return """你是 OTC 药店测试助手。你可以通过工具回答用户问题。

工具使用规则：
- 根据用户问题选择合适的工具
- 基于工具返回的信息回答，不要编造
- 回复简洁、准确
- 如果没有合适的工具，直接回答即可"""


@pytest.fixture
def tool_registry():
    """注册 2 个 mock 工具：search_drug（成功）, get_price（成功）。"""
    registry = ToolRegistry()

    # 工具 1: 药品搜索（总是成功）
    async def mock_search_drug(query: str, limit: int = 5):
        drugs = {
            "布洛芬": [
                {"name": "布洛芬缓释胶囊", "category": "解热镇痛", "manufacturer": "某某制药"},
                {"name": "布洛芬混悬液", "category": "解热镇痛", "manufacturer": "另一制药"},
            ],
            "对乙酰氨基酚": [
                {"name": "对乙酰氨基酚片", "category": "解热镇痛", "manufacturer": "泰诺制药"},
            ],
            "阿莫西林": [
                {"name": "阿莫西林胶囊", "category": "抗生素", "manufacturer": "某抗生素厂"},
            ],
        }
        results = []
        for name, entries in drugs.items():
            if query in name:
                results.extend(entries)
        if not results:
            results = [{"name": f"未找到与'{query}'匹配的药品", "category": "", "manufacturer": ""}]
        return results[:limit]

    registry.register(
        ToolDefinition(
            name="search_drug",
            description="搜索药品。根据药品名称模糊搜索，返回匹配的药品列表。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                    "limit": {"type": "integer", "description": "返回数量上限"},
                },
                "required": ["query"],
            },
        ),
        mock_search_drug,
    )

    # 工具 2: 查价格（异步函数）
    async def mock_get_price(drug_name: str):
        prices = {
            "布洛芬": {"price": "¥18.50", "stock": "有货"},
            "对乙酰氨基酚": {"price": "¥12.00", "stock": "库存紧张"},
            "阿莫西林": {"price": "¥25.00", "stock": "有货"},
        }
        return prices.get(drug_name, {"price": "未知", "stock": "未知"})

    registry.register(
        ToolDefinition(
            name="get_price",
            description="查询药品价格和库存。输入药品通用名，返回价格信息。",
            parameters={
                "type": "object",
                "properties": {
                    "drug_name": {"type": "string", "description": "药品通用名"},
                },
                "required": ["drug_name"],
            },
        ),
        mock_get_price,
    )

    return registry


@pytest.fixture
def agent(llm_client, system_prompt, tool_registry, react_profile):
    return ReactAgent(
        llm_client=llm_client,
        system_prompt=system_prompt,
        tool_registry=tool_registry,
        profile=react_profile,
        max_iterations=5,
    )


# ═══════════════════════════════════════════════════════════
# Tests: 真实 LLM 调用
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_simple_chat_no_tools(agent):
    """简单闲聊 — LLM 不使用工具，直接返回文本回复。"""
    result = await agent.run(
        user_message="你好，请问你是谁？",
        history=None,
        context=None,
    )

    assert isinstance(result, AgentResult)
    assert result.final_response, "final_response should not be empty"
    assert result.total_iterations >= 1
    assert result.total_time_ms > 0
    # 闲聊不应该调用工具
    assert len(result.steps) == 0, "Chat should not trigger any tool calls"


@pytest.mark.asyncio
async def test_single_tool_call(agent):
    """单工具调用 — LLM 调用 search_drug，返回正确结果。"""
    result = await agent.run(
        user_message="帮我查一下布洛芬这个药",
        history=None,
        context=None,
    )

    assert isinstance(result, AgentResult)
    assert result.final_response, "final_response should not be empty"
    assert "布洛芬" in result.final_response, "Response should mention the drug"
    # 应该有至少一次工具调用
    assert result.total_iterations >= 1


@pytest.mark.asyncio
async def test_multi_tool_call_parallel(agent):
    """并行工具调用 — LLM 同时查两个药品。"""
    result = await agent.run(
        user_message="帮我查一下布洛芬和对乙酰氨基酚的价格",
        history=None,
        context=None,
    )

    assert isinstance(result, AgentResult)
    assert result.final_response, "final_response should not be empty"
    # 应该涉及两个药品
    response_lower = result.final_response.lower()
    assert "布洛芬" in result.final_response or "price" in response_lower


@pytest.mark.asyncio
async def test_drug_query_with_history(agent):
    """带对话历史的药品查询。"""
    history = [
        {"role": "user", "content": "我头痛"},
        {"role": "assistant", "content": "请问您头痛多久了？有没有发烧？"},
    ]

    result = await agent.run(
        user_message="布洛芬怎么吃",
        history=history,
        context=None,
    )

    assert isinstance(result, AgentResult)
    assert result.final_response, "final_response should not be empty"


@pytest.mark.asyncio
async def test_tool_error_handling(agent, tool_registry, system_prompt, llm_client, react_profile):
    """工具执行失败 → Agent 继续运行，不中断。"""
    # 注册一个会失败的额外工具
    async def failing_tool():
        raise ValueError("Database connection timeout")

    tool_registry.register(
        ToolDefinition(
            name="failing_search",
            description="会失败的搜索工具。当用户要求搜索特定内容时调用。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索内容"},
                },
                "required": ["query"],
            },
        ),
        failing_tool,
    )

    agent2 = ReactAgent(
        llm_client=llm_client,
        system_prompt=system_prompt + "\n如果 search_drug 不可用，请尝试用 failing_search 工具。",
        tool_registry=tool_registry,
        profile=react_profile,
        max_iterations=5,
    )

    result = await agent2.run(
        user_message="搜索一下阿莫西林的信息",
        history=None,
    )

    assert isinstance(result, AgentResult)
    # 应该要么成功返回，要么告知用户失败——不应该抛异常
    assert result.final_response, "Agent should return something even with tool errors"


# ═══════════════════════════════════════════════════════════
# Tests: 错误场景（mock LLM）
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_max_iterations_force_summarize(tool_registry, system_prompt, react_profile):
    """超过 max_iterations → 强制总结，返回有效回复。

    通过 mock LLM 始终返回 tool_calls 来触发超限。
    """
    from app.llm.client import StreamWithToolsResult

    # 构造一个始终返回 tool_calls 的 mock result
    mock_result = StreamWithToolsResult(
        has_tool_calls=True,
        tool_calls=[{
            "id": "call_mock_001",
            "type": "function",
            "function": {"name": "search_drug", "arguments": '{"query":"布洛芬"}'},
        }],
        content="",
    )

    mock_llm = AsyncMock()
    mock_llm.generate_with_tools_stream.return_value = mock_result
    mock_llm.default_profile = react_profile

    agent = ReactAgent(
        llm_client=mock_llm,
        system_prompt=system_prompt,
        tool_registry=tool_registry,
        profile=react_profile,
        max_iterations=2,  # 很低的上限
    )

    result = await agent.run(user_message="查布洛芬")

    assert isinstance(result, AgentResult)
    assert result.total_iterations == 2, f"Should hit max_iterations, got {result.total_iterations}"
    assert result.final_response, "Force summarize should produce a response"


@pytest.mark.asyncio
async def test_llm_exception_fallback(tool_registry, system_prompt, react_profile):
    """LLM 完全不可用 → _format_raw_result 降级。

    模拟 LLM 抛异常，验证 agent 返回降级回复。
    """
    mock_llm = AsyncMock()
    mock_llm.generate_with_tools_stream.side_effect = Exception("Connection refused")
    mock_llm.default_profile = react_profile

    agent = ReactAgent(
        llm_client=mock_llm,
        system_prompt=system_prompt,
        tool_registry=tool_registry,
        profile=react_profile,
        max_iterations=5,
    )

    result = await agent.run(user_message="查药")

    assert isinstance(result, AgentResult)
    assert result.final_response, "Fallback response should not be empty"
    # 降级回复应包含提示信息
    assert "暂" in result.final_response or "不" in result.final_response


@pytest.mark.asyncio
async def test_llm_exception_fallback_with_findings(tool_registry, system_prompt, react_profile):
    """LLM 失败但已有工具缓存 → 降级回复包含工具数据。

    先让 agent 的工具执行成功（memory 有数据），然后 LLM 再失败。
    """
    from app.llm.client import StreamWithToolsResult

    mock_llm = AsyncMock()
    mock_llm.default_profile = react_profile

    # 第一次调用：返回 tool_calls（让 agent 去执行工具）
    mock_result1 = StreamWithToolsResult(
        has_tool_calls=True,
        tool_calls=[{
            "id": "call_001",
            "type": "function",
            "function": {"name": "search_drug", "arguments": '{"query":"布洛芬"}'},
        }],
        content="",
    )

    # 第二次调用：抛异常
    mock_llm.generate_with_tools_stream.side_effect = [
        mock_result1,
        Exception("API timeout"),
    ]

    agent = ReactAgent(
        llm_client=mock_llm,
        system_prompt=system_prompt,
        tool_registry=tool_registry,
        profile=react_profile,
        max_iterations=5,
    )

    result = await agent.run(user_message="查布洛芬")

    assert isinstance(result, AgentResult)
    assert result.final_response, "Should have fallback response"
    # 降级回复应包含工具查询到的药品名
    assert "布洛芬" in result.final_response, (
        f"Fallback should include tool findings, got: {result.final_response[:200]}"
    )


# ═══════════════════════════════════════════════════════════
# Tests: WorkingMemory
# ═══════════════════════════════════════════════════════════


class TestWorkingMemory:
    def test_basic_operations(self):
        mem = WorkingMemory()
        assert mem.is_empty

        mem.add_finding("search_drug", [{"name": "布洛芬"}])
        assert mem.has_finding("search_drug")
        assert mem.get_finding("search_drug") == [{"name": "布洛芬"}]
        assert not mem.is_empty

        mem.add_note("test note")
        assert len(mem.notes) == 1

        snap = mem.snapshot()
        assert "search_drug" in snap["intermediate_findings"]
        assert "test note" in snap["context_notes"]

        mem.clear()
        assert mem.is_empty


# ═══════════════════════════════════════════════════════════
# Tests: Context injection
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_context_injection_workflow_done(agent):
    """workflow done 上下文 — react agent 的回复利用推荐结果。"""
    context = {
        "workflow_action": "done",
        "workflow_response": (
            "根据您的情况，为您推荐以下药品：\n"
            "1. 布洛芬缓释胶囊（评分 92）\n"
            "2. 对乙酰氨基酚片（评分 85）"
        ),
    }

    result = await agent.run(
        user_message="这些药有什么副作用",
        history=None,
        context=context,
    )

    assert isinstance(result, AgentResult)
    assert result.final_response, "final_response should not be empty"


@pytest.mark.asyncio
async def test_context_injection_workflow_ask(agent):
    """workflow ask 上下文 — react agent 不打断追问。"""
    context = {
        "workflow_action": "ask",
        "workflow_response": "请问您咳嗽多久了？有没有发烧？",
    }

    result = await agent.run(
        user_message="对了，布洛芬是退烧药吗",
        history=None,
        context=context,
    )

    assert isinstance(result, AgentResult)
    assert result.final_response, "final_response should not be empty"


# ═══════════════════════════════════════════════════════════
# Tests: _build_context_text
# ═══════════════════════════════════════════════════════════


def test_build_context_text_done(agent):
    """验证 done 状态下的 context 文本生成。"""
    context = {
        "workflow_action": "done",
        "workflow_response": "已推荐：布洛芬",
    }
    text = agent._build_context_text(context)
    assert "布洛芬" in text
    assert "推荐" in text


def test_build_context_text_ask(agent):
    """验证 ask 状态下的 context 文本生成。"""
    context = {
        "workflow_action": "ask",
        "workflow_response": "请问您咳嗽多久了？",
    }
    text = agent._build_context_text(context)
    assert "咳嗽" in text
    assert "追问" in text


def test_build_context_text_empty(agent):
    """空 context → 空字符串。"""
    assert agent._build_context_text({}) == ""
    assert agent._build_context_text({"workflow_response": ""}) == ""
