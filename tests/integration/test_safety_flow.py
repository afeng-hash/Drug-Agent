"""Integration tests for the safety block flow."""

import pytest


@pytest.mark.asyncio
async def test_safety_block_stops_recommendation():
    """When safety check BLOCKs, the flow should not reach recommend."""
    from app.rules.definitions import register_all_rules
    from app.rules.engine import RuleEngine

    engine = RuleEngine()
    register_all_rules(engine)

    # High fever 4 days → R1 BLOCK
    slots = {
        "symptoms": [{"name": "发热", "severity": "重度"}],
        "temperature": 39.5,
        "duration_days": 4,
        "age": 35,
        "medications_taken": [],
        "special_population": None,
        "chronic_conditions": [],
        "allergies": [],
        "other_symptoms": [],
    }

    result = engine.check(slots)
    assert result.verdict == "BLOCK"
    assert len(result.message) > 0
    assert "就医" in result.message


@pytest.mark.asyncio
async def test_safety_block_message_contains_guidance():
    """Block message should direct user to seek medical care."""
    from app.rules.definitions import register_all_rules
    from app.rules.engine import RuleEngine

    engine = RuleEngine()
    register_all_rules(engine)

    slots = {
        "symptoms": [{"name": "头痛", "severity": "剧烈"}],
        "temperature": 38.0,
        "duration_days": 1,
        "age": 45,
        "medications_taken": [],
        "special_population": None,
        "chronic_conditions": [],
        "allergies": [],
        "other_symptoms": ["呼吸困难"],
    }

    result = engine.check(slots)
    assert result.verdict == "BLOCK"
    # Message should not contain any drug names
    assert "布洛芬" not in result.message
    assert "对乙酰氨基酚" not in result.message


@pytest.mark.asyncio
async def test_filter_excludes_drugs_then_passes():
    """When only FILTER rules trigger, verdict is FILTER not BLOCK."""
    from app.rules.definitions import register_all_rules
    from app.rules.engine import RuleEngine

    engine = RuleEngine()
    register_all_rules(engine)

    # Child → R7 triggers FILTER for aspirin
    slots = {
        "symptoms": [{"name": "发热"}],
        "temperature": 38.5,
        "duration_days": 2,
        "age": 8,
        "medications_taken": [],
        "special_population": None,
        "chronic_conditions": [],
        "allergies": [],
        "other_symptoms": [],
    }

    result = engine.check(slots, ["阿司匹林", "布洛芬"])
    assert result.verdict == "FILTER"
    assert "阿司匹林" in result.excluded_drugs
    assert "布洛芬" not in result.excluded_drugs
