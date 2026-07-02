"""Shared pytest fixtures."""

import pytest

from app.rules.definitions import register_all_rules
from app.rules.engine import RuleEngine


@pytest.fixture
def rule_engine() -> RuleEngine:
    """A RuleEngine with all 7 rules registered."""
    engine = RuleEngine()
    register_all_rules(engine)
    return engine


@pytest.fixture
def empty_slots() -> dict:
    """Default empty consult slots."""
    return {
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


@pytest.fixture
def normal_adult_slots(empty_slots) -> dict:
    """Typical adult cold/fever slots — should PASS all rules."""
    return {
        **empty_slots,
        "symptoms": [
            {"name": "头痛", "location": "额头", "severity": "中度", "onset": "2天前"}
        ],
        "temperature": 38.2,
        "duration_days": 2,
        "age": 28,
    }


@pytest.fixture
def high_fever_slots(empty_slots) -> dict:
    """High fever ≥ 39°C for 4 days — should trigger R1."""
    return {
        **empty_slots,
        "symptoms": [{"name": "发热", "severity": "重度"}],
        "temperature": 39.5,
        "duration_days": 4,
        "age": 35,
    }


@pytest.fixture
def pregnant_fever_slots(empty_slots) -> dict:
    """Pregnant + fever ≥ 38.5°C — should trigger R3."""
    return {
        **empty_slots,
        "symptoms": [{"name": "发热"}],
        "temperature": 38.8,
        "duration_days": 1,
        "age": 30,
        "special_population": "pregnant",
    }


@pytest.fixture
def child_fever_slots(empty_slots) -> dict:
    """Child with fever — should trigger R7 (aspirin FILTER)."""
    return {
        **empty_slots,
        "symptoms": [{"name": "发热"}],
        "temperature": 38.5,
        "duration_days": 2,
        "age": 8,
    }


@pytest.fixture
def ibuprofen_allergy_slots(empty_slots) -> dict:
    """User allergic to ibuprofen — should trigger R6."""
    return {
        **empty_slots,
        "symptoms": [{"name": "头痛"}],
        "temperature": 37.5,
        "duration_days": 1,
        "age": 25,
        "allergies": ["布洛芬"],
    }


@pytest.fixture
def emergency_slots(empty_slots) -> dict:
    """Emergency signs — should trigger R4."""
    return {
        **empty_slots,
        "symptoms": [{"name": "头痛", "severity": "剧烈"}],
        "other_symptoms": ["呼吸困难", "胸痛"],
        "temperature": 38.0,
        "duration_days": 1,
        "age": 45,
    }
