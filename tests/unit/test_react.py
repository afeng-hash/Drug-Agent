"""Unit tests for react_node and router."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agent.react.schemas import AgentResult
from app.graph.nodes.react import react_node
from app.graph.router import (
    route_after_consult,
    route_after_dispatcher,
    route_after_inventory,
    route_after_safety,
)


# ═══════════════════════════════════════════════════════════
# Router tests
# ═══════════════════════════════════════════════════════════

class TestRouteAfterDispatcher:
    def test_has_workflow_goes_to_consult(self):
        state = {"dispatcher_result": {
            "actions": [{"action": "workflow", "intent": "describe_symptom", "priority": 1}],
        }}
        assert route_after_dispatcher(state) == "consult"

    def test_only_react_goes_to_react(self):
        state = {"dispatcher_result": {
            "actions": [{"action": "react", "intent": "ask_drug", "priority": 1}],
        }}
        assert route_after_dispatcher(state) == "react"

    def test_mixed_goes_to_consult(self):
        """workflow 存在时优先走 consult。"""
        state = {"dispatcher_result": {
            "actions": [
                {"action": "workflow", "intent": "describe_symptom", "priority": 1},
                {"action": "react", "intent": "ask_drug", "priority": 2},
            ],
        }}
        assert route_after_dispatcher(state) == "consult"

    def test_empty_goes_to_react(self):
        """空计划默认走 react。"""
        state = {"dispatcher_result": {"actions": []}}
        assert route_after_dispatcher(state) == "react"


class TestRouteAfterConsult:
    def test_done_goes_to_safety(self):
        state = {
            "consult_next_action": "done",
            "dispatcher_result": {"actions": []},
        }
        assert route_after_consult(state) == "safety_block"

    def test_ask_no_react_goes_to_end(self):
        state = {
            "consult_next_action": "ask",
            "dispatcher_result": {"actions": [
                {"action": "workflow", "intent": "describe_symptom", "priority": 1},
            ]},
        }
        assert route_after_consult(state) == "end"

    def test_ask_with_react_goes_to_react(self):
        state = {
            "consult_next_action": "ask",
            "dispatcher_result": {"actions": [
                {"action": "workflow", "intent": "describe_symptom", "priority": 1},
                {"action": "react", "intent": "ask_drug", "priority": 2},
            ]},
        }
        assert route_after_consult(state) == "react"


class TestRouteAfterSafety:
    def test_pass_goes_to_recommend(self):
        state = {"safety_result": {"verdict": "PASS"}}
        assert route_after_safety(state) == "recommend"

    def test_block_goes_to_end(self):
        state = {"safety_result": {"verdict": "BLOCK"}}
        assert route_after_safety(state) == "end"


class TestRouteAfterInventory:
    def test_has_react_goes_to_react(self):
        state = {"dispatcher_result": {
            "actions": [
                {"action": "workflow", "intent": "describe_symptom", "priority": 1},
                {"action": "react", "intent": "ask_drug", "priority": 2},
            ],
        }}
        assert route_after_inventory(state) == "react"

    def test_no_react_goes_to_end(self):
        state = {"dispatcher_result": {
            "actions": [{"action": "workflow", "intent": "describe_symptom", "priority": 1}],
        }}
        assert route_after_inventory(state) == "end"


# ═══════════════════════════════════════════════════════════
# React node tests
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_react_node_pure_react():
    """纯 react → ReactAgent 被调用，返回结果。"""
    state = {
        "session_id": "test",
        "messages": [{"role": "user", "content": "布洛芬有什么副作用"}],
        "phase": "intake",
        "dispatcher_result": {
            "actions": [
                {"action": "react", "intent": "ask_drug",
                 "query": "布洛芬有什么副作用", "priority": 1},
            ],
        },
        "consult_slots": {},
        "consult_next_action": "",
        "recommendations": [],
        "response": "",
    }

    mock_agent = MagicMock()
    mock_agent.run = AsyncMock(return_value=AgentResult(
        final_response="布洛芬常见副作用包括胃肠道不适、头晕等。",
        steps=[],
        total_iterations=1,
        total_time_ms=500.0,
    ))

    result = await react_node(state, mock_agent)

    assert "布洛芬" in result["response"]
    assert result["phase"] == "ended"
    mock_agent.run.assert_called_once()


@pytest.mark.asyncio
async def test_react_node_after_workflow_ask():
    """workflow ask 后 → react 拼接追问语和自己的回复。"""
    state = {
        "session_id": "test",
        "messages": [{"role": "user", "content": "咳嗽吃啥药，连花清瘟能吃吗"}],
        "phase": "consulting",
        "dispatcher_result": {
            "actions": [
                {"action": "workflow", "intent": "describe_symptom", "priority": 1},
                {"action": "react", "intent": "ask_drug",
                 "query": "连花清瘟能吃吗", "priority": 2},
            ],
        },
        "consult_slots": {"symptoms": [{"name": "咳嗽"}]},
        "consult_next_action": "ask",
        "recommendations": [],
        "response": "请问您咳嗽多久了？有没有发烧？",
    }

    mock_agent = MagicMock()
    mock_agent.run = AsyncMock(return_value=AgentResult(
        final_response="关于连花清瘟胶囊，它主要用于治疗流感引起的发热、咳嗽等症状。",
        steps=[],
        total_iterations=1,
        total_time_ms=600.0,
    ))

    result = await react_node(state, mock_agent)

    # 应同时包含追问语和 react 回复
    assert "咳嗽" in result["response"]
    assert "连花清瘟" in result["response"]
    # 追问后保持 consulting 阶段
    assert result["phase"] == "consulting"


@pytest.mark.asyncio
async def test_react_node_after_workflow_done():
    """workflow done 后 → react 拼接推荐和自己的回复。"""
    state = {
        "session_id": "test",
        "messages": [{"role": "user", "content": "布洛芬有什么副作用"}],
        "phase": "recommending",
        "dispatcher_result": {
            "actions": [
                {"action": "workflow", "intent": "describe_symptom", "priority": 1},
                {"action": "react", "intent": "ask_drug",
                 "query": "布洛芬有什么副作用", "priority": 2},
            ],
        },
        "consult_slots": {"symptoms": [{"name": "头痛"}]},
        "consult_next_action": "done",
        "recommendations": [{"drug_id": 1, "generic_name": "布洛芬"}],
        "response": "推荐：布洛芬缓释胶囊（评分92）\n\n库存：¥18.50 有货",
    }

    mock_agent = MagicMock()
    mock_agent.run = AsyncMock(return_value=AgentResult(
        final_response="布洛芬是一种解热镇痛药，常见副作用包括...",
        steps=[],
        total_iterations=1,
        total_time_ms=500.0,
    ))

    result = await react_node(state, mock_agent)

    assert "推荐" in result["response"]
    assert "布洛芬" in result["response"]
    assert result["phase"] == "ended"
    # react_agent 收到了 workflow 上下文
    call_kwargs = mock_agent.run.call_args.kwargs
    assert call_kwargs["context"] is not None
    assert call_kwargs["context"]["workflow_action"] == "done"


@pytest.mark.asyncio
async def test_react_node_with_state_proxy():
    """state_proxy 在 react 调用前被更新。"""
    state = {
        "session_id": "test",
        "messages": [{"role": "user", "content": "这个药怎么样"}],
        "phase": "recommending",
        "dispatcher_result": {
            "actions": [
                {"action": "react", "intent": "ask_drug",
                 "query": "这个药怎么样", "priority": 1},
            ],
        },
        "consult_slots": {
            "symptoms": [{"name": "头痛"}],
            "age": 28,
            "allergies": ["阿司匹林"],
        },
        "consult_next_action": "done",
        "recommendations": [{"drug_id": 1, "generic_name": "布洛芬"}],
        "response": "",
    }

    state_proxy = MagicMock()
    state_proxy.recommendations = []
    state_proxy.user_profile = {}

    mock_agent = MagicMock()
    mock_agent.run = AsyncMock(return_value=AgentResult(
        final_response="布洛芬对您来说是安全的",
        steps=[],
        total_iterations=1,
        total_time_ms=500.0,
    ))

    await react_node(state, mock_agent, state_proxy=state_proxy)

    # state_proxy 被更新
    assert state_proxy.recommendations == [{"drug_id": 1, "generic_name": "布洛芬"}]
    assert state_proxy.user_profile["age"] == 28
    assert state_proxy.user_profile["allergies"] == ["阿司匹林"]


@pytest.mark.asyncio
async def test_react_node_no_query_uses_last_message():
    """没有显式 query → 取最后一条用户消息。"""
    state = {
        "session_id": "test",
        "messages": [{"role": "user", "content": "你好"}],
        "phase": "intake",
        "dispatcher_result": {
            "actions": [
                {"action": "react", "intent": "chat", "query": "", "priority": 1},
            ],
        },
        "consult_slots": {},
        "consult_next_action": "",
        "recommendations": [],
        "response": "",
    }

    mock_agent = MagicMock()
    mock_agent.run = AsyncMock(return_value=AgentResult(
        final_response="您好！有什么可以帮您的？",
        steps=[],
        total_iterations=1,
        total_time_ms=300.0,
    ))

    result = await react_node(state, mock_agent)

    assert result["response"]
    mock_agent.run.assert_called_once()
    # 验证 query 被正确提取
    assert mock_agent.run.call_args.kwargs["user_message"] == "你好"
