"""Unit tests for the Dispatcher node."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.graph.nodes.dispatcher import DispatcherDecision, dispatcher_node


@pytest.mark.asyncio
async def test_dispatcher_routes_symptom_to_consult():
    """User describes symptoms → route=consult."""
    state = {
        "session_id": "test-session",
        "messages": [{"role": "user", "content": "我头疼流鼻涕两天了"}],
        "phase": "intake",
        "previous_phase": None,
        "consult_slots": {
            "symptoms": [],
            "temperature": None,
            "duration_days": None,
            "medications_taken": [],
            "special_population": None,
            "age": None,
            "chronic_conditions": [],
            "allergies": [],
            "other_symptoms": [],
        },
        "dispatcher_result": {},
    }

    mock_llm = AsyncMock()
    mock_llm.generate_structured.return_value = DispatcherDecision(
        route="consult",
        intent="describe_symptom",
        params={},
    )

    result = await dispatcher_node(state, mock_llm)
    assert result["dispatcher_result"]["route"] == "consult"
    assert result["dispatcher_result"]["intent"] == "describe_symptom"


@pytest.mark.asyncio
async def test_dispatcher_routes_drug_query_to_explain():
    """User asks about a drug → route=explain."""
    state = {
        "session_id": "test-session",
        "messages": [{"role": "user", "content": "布洛芬有什么副作用？"}],
        "phase": "consulting",
        "previous_phase": None,
        "consult_slots": {
            "symptoms": [{"name": "头痛"}],
            "temperature": None,
            "duration_days": 1,
            "medications_taken": [],
            "special_population": None,
            "age": None,
            "chronic_conditions": [],
            "allergies": [],
            "other_symptoms": [],
        },
        "dispatcher_result": {},
    }

    mock_llm = AsyncMock()
    mock_llm.generate_structured.return_value = DispatcherDecision(
        route="explain",
        intent="ask_drug",
        params={"drug_name": "布洛芬"},
    )

    result = await dispatcher_node(state, mock_llm)
    assert result["dispatcher_result"]["route"] == "explain"
    # Should record previous_phase to return to consulting later
    assert result["previous_phase"] == "consulting"


@pytest.mark.asyncio
async def test_dispatcher_routes_give_up_to_end():
    """User gives up → route=end."""
    state = {
        "session_id": "test-session",
        "messages": [{"role": "user", "content": "算了去医院吧"}],
        "phase": "consulting",
        "previous_phase": None,
        "consult_slots": {},
        "dispatcher_result": {},
    }

    mock_llm = AsyncMock()
    mock_llm.generate_structured.return_value = DispatcherDecision(
        route="end",
        intent="give_up",
        params={},
    )

    result = await dispatcher_node(state, mock_llm)
    assert result["dispatcher_result"]["route"] == "end"
    assert result["dispatcher_result"]["intent"] == "give_up"


@pytest.mark.asyncio
async def test_dispatcher_fallback_on_llm_error():
    """When LLM fails, dispatcher falls back to consult."""
    state = {
        "session_id": "test-session",
        "messages": [{"role": "user", "content": "你好"}],
        "phase": "intake",
        "previous_phase": None,
        "consult_slots": {},
        "dispatcher_result": {},
    }

    mock_llm = AsyncMock()
    mock_llm.generate_structured.side_effect = Exception("LLM unavailable")

    result = await dispatcher_node(state, mock_llm)
    assert result["dispatcher_result"]["route"] == "consult"
    assert result["dispatcher_result"]["intent"] == "fallback"
