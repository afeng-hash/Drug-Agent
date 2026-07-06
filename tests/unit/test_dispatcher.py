"""Unit tests for the Dispatcher node (v2: actions[] format)."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.graph.nodes.dispatcher import ActionItem, DispatcherDecision, dispatcher_node


@pytest.mark.asyncio
async def test_dispatcher_routes_symptom_to_workflow():
    """User describes symptoms → actions: [{action: "workflow"}]."""
    state = {
        "session_id": "test-session",
        "messages": [{"role": "user", "content": "我头疼流鼻涕两天了"}],
        "phase": "intake",
        "consult_slots": {
            "symptoms": [],
            "temperature": None,
            "duration_days": None,
            "medications_taken": [],
            "special_population": None,
            "age": None,
            "chronic_conditions": [],
            "allergies": [],
        },
        "dispatcher_result": {},
    }

    mock_llm = AsyncMock()
    mock_llm.generate_structured.return_value = DispatcherDecision(
        actions=[
            ActionItem(action="workflow", intent="describe_symptom", priority=1),
        ],
    )

    result = await dispatcher_node(state, mock_llm)
    actions = result["dispatcher_result"]["actions"]
    assert len(actions) == 1
    assert actions[0]["action"] == "workflow"
    assert actions[0]["intent"] == "describe_symptom"


@pytest.mark.asyncio
async def test_dispatcher_routes_drug_query_to_react():
    """User asks about a drug → actions: [{action: "react"}].

    In v2, drug queries are handled by ReactAgent (no more "explain" route).
    """
    state = {
        "session_id": "test-session",
        "messages": [{"role": "user", "content": "布洛芬有什么副作用？"}],
        "phase": "consulting",
        "consult_slots": {
            "symptoms": [{"name": "头痛"}],
            "temperature": None,
            "duration_days": 1,
            "medications_taken": [],
            "special_population": None,
            "age": None,
            "chronic_conditions": [],
            "allergies": [],
        },
        "dispatcher_result": {},
    }

    mock_llm = AsyncMock()
    mock_llm.generate_structured.return_value = DispatcherDecision(
        actions=[
            ActionItem(action="react", intent="ask_drug",
                       query="布洛芬有什么副作用", priority=1),
        ],
    )

    result = await dispatcher_node(state, mock_llm)
    actions = result["dispatcher_result"]["actions"]
    assert len(actions) == 1
    assert actions[0]["action"] == "react"
    assert actions[0]["intent"] == "ask_drug"
    assert "布洛芬" in actions[0]["query"]


@pytest.mark.asyncio
async def test_dispatcher_routes_give_up_to_react():
    """User gives up → actions: [{action: "react", intent: "give_up"}]."""
    state = {
        "session_id": "test-session",
        "messages": [{"role": "user", "content": "算了去医院吧"}],
        "phase": "consulting",
        "consult_slots": {},
        "dispatcher_result": {},
    }

    mock_llm = AsyncMock()
    mock_llm.generate_structured.return_value = DispatcherDecision(
        actions=[
            ActionItem(action="react", intent="give_up", priority=1),
        ],
    )

    result = await dispatcher_node(state, mock_llm)
    actions = result["dispatcher_result"]["actions"]
    assert len(actions) == 1
    assert actions[0]["action"] == "react"
    assert actions[0]["intent"] == "give_up"


@pytest.mark.asyncio
async def test_dispatcher_fallback_on_llm_error():
    """When LLM fails, dispatcher falls back to react (safe default)."""
    state = {
        "session_id": "test-session",
        "messages": [{"role": "user", "content": "你好"}],
        "phase": "intake",
        "consult_slots": {},
        "dispatcher_result": {},
    }

    mock_llm = AsyncMock()
    mock_llm.generate_structured.side_effect = Exception("LLM unavailable")

    result = await dispatcher_node(state, mock_llm)
    actions = result["dispatcher_result"]["actions"]
    assert len(actions) == 1
    assert actions[0]["action"] == "react"
    assert actions[0]["intent"] == "fallback"


@pytest.mark.asyncio
async def test_dispatcher_mixed_intent():
    """Mixed intent (symptom + drug) → actions: [workflow, react]."""
    state = {
        "session_id": "test-session",
        "messages": [{"role": "user", "content": "咳嗽吃什么药，布洛芬有什么作用"}],
        "phase": "intake",
        "consult_slots": {},
        "dispatcher_result": {},
    }

    mock_llm = AsyncMock()
    mock_llm.generate_structured.return_value = DispatcherDecision(
        actions=[
            ActionItem(action="workflow", intent="describe_symptom", priority=1),
            ActionItem(action="react", intent="ask_drug",
                       query="布洛芬有什么作用", priority=2),
        ],
    )

    result = await dispatcher_node(state, mock_llm)
    actions = result["dispatcher_result"]["actions"]
    assert len(actions) == 2
    assert actions[0]["action"] == "workflow"
    assert actions[0]["priority"] == 1
    assert actions[1]["action"] == "react"
    assert actions[1]["priority"] == 2
