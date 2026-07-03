"""Unit tests for the Consult Agent (ReAct symptom gathering)."""

from unittest.mock import AsyncMock

import pytest

from app.agent.consult_agent import ConsultResult, run_consult


@pytest.mark.asyncio
async def test_consult_asks_when_slots_insufficient():
    """When slots are empty, agent should ask a follow-up question."""
    llm_client = AsyncMock()
    llm_client.generate_structured.return_value = ConsultResult(
        updated_slots={
            "symptoms": [{"name": "头痛", "location": "额头", "severity": "中度"}],
            "temperature": None,
            "duration_days": 2,
            "medications_taken": [],
            "special_population": None,
            "age": None,
            "chronic_conditions": [],
            "allergies": [],
            "other_symptoms": ["流鼻涕"],
        },
        response="您有没有量体温？发烧吗？",
        next_action="ask",
        summary="",
    )

    messages = [
        {"role": "user", "content": "我头疼两天了，还流鼻涕"},
    ]
    current_slots = {
        "symptoms": [],
        "temperature": None,
        "duration_days": None,
        "medications_taken": [],
        "special_population": None,
        "age": None,
        "chronic_conditions": [],
        "allergies": [],
        "other_symptoms": [],
    }

    result = await run_consult(llm_client, messages, current_slots, max_rounds=6)
    assert result.next_action == "ask"
    assert len(result.response) > 0
    # Slots should be updated with new info
    assert len(result.updated_slots["symptoms"]) >= 1
    assert result.updated_slots["duration_days"] == 2


@pytest.mark.asyncio
async def test_consult_done_when_slots_sufficient():
    """When enough info is gathered, agent should mark done."""
    llm_client = AsyncMock()
    llm_client.generate_structured.return_value = ConsultResult(
        updated_slots={
            "symptoms": [{"name": "头痛"}, {"name": "发热"}],
            "temperature": 38.2,
            "duration_days": 2,
            "medications_taken": [],
            "special_population": None,
            "age": 28,
            "chronic_conditions": [],
            "allergies": [],
            "other_symptoms": ["流鼻涕"],
        },
        response="好的，我已经了解了您的情况，让我为您推荐药品。",
        next_action="done",
        summary="28岁成人，头痛伴发热38.2°C持续2天，无药物过敏，非特殊人群。",
    )

    messages = [
        {"role": "user", "content": "我头疼两天了，体温38.2度"},
        {"role": "assistant", "content": "请问您的年龄？有没有药物过敏？"},
        {"role": "user", "content": "28岁，没有过敏"},
    ]
    current_slots = {
        "symptoms": [{"name": "头痛"}, {"name": "发热"}],
        "temperature": 38.2,
        "duration_days": 2,
        "medications_taken": [],
        "special_population": None,
        "age": 28,
        "chronic_conditions": [],
        "allergies": [],
        "other_symptoms": [],
    }

    result = await run_consult(llm_client, messages, current_slots, max_rounds=6)
    assert result.next_action == "done"
    assert len(result.summary) > 0


@pytest.mark.asyncio
async def test_consult_forces_done_at_max_rounds():
    """At max rounds, agent should force done even if LLM would ask more."""
    llm_client = AsyncMock()

    current_slots = {
        "symptoms": [{"name": "头痛"}],
        "temperature": 37.5,
        "duration_days": 1,
        "medications_taken": [],
        "special_population": None,
        "age": None,
        "chronic_conditions": [],
        "allergies": [],
        "other_symptoms": [],
    }

    # 6 assistant messages = max rounds
    messages = [
        {"role": "user", "content": "头疼"},
        {"role": "assistant", "content": "发烧吗？"},
        {"role": "user", "content": "不发烧"},
        {"role": "assistant", "content": "多久了？"},
        {"role": "user", "content": "一天"},
        {"role": "assistant", "content": "多大年龄？"},
        {"role": "user", "content": "不方便说"},
        {"role": "assistant", "content": "有其他症状吗？"},
        {"role": "user", "content": "没有"},
        {"role": "assistant", "content": "有没有过敏？"},
        {"role": "user", "content": "没有"},
        {"role": "assistant", "content": "吃过药吗？"},
    ]

    # 显式传入 consult_rounds=6（已达到上限），不再依赖从 messages 内容反推轮数
    result = await run_consult(
        llm_client, messages, current_slots, max_rounds=6, consult_rounds=6,
    )
    assert result.next_action == "done"
    # LLM should NOT have been called
    llm_client.generate_structured.assert_not_called()


@pytest.mark.asyncio
async def test_consult_merges_slots_without_data_loss():
    """Slots should merge: new info added, existing info preserved."""
    llm_client = AsyncMock()
    # LLM only returns partial slots (common behavior)
    llm_client.generate_structured.return_value = ConsultResult(
        updated_slots={
            "symptoms": [{"name": "头痛"}],
            "temperature": 38.0,
            "duration_days": None,  # LLM didn't update this
        },
        response="您多大年龄？",
        next_action="ask",
        summary="",
    )

    current_slots = {
        "symptoms": [{"name": "头痛"}],
        "temperature": None,
        "duration_days": 3,  # Previously collected
        "medications_taken": ["感康"],
        "special_population": None,
        "age": None,
        "chronic_conditions": [],
        "allergies": [],
        "other_symptoms": [],
    }

    messages = [{"role": "user", "content": "我发烧了"}]

    result = await run_consult(llm_client, messages, current_slots)
    # Existing value should be preserved
    assert result.updated_slots["duration_days"] == 3
    # New value should be updated
    assert result.updated_slots["temperature"] == 38.0
    # Existing list preserved
    assert "感康" in result.updated_slots["medications_taken"]
