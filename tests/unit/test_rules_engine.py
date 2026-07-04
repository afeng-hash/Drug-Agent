"""Unit tests for the safety rule engine and 5 BLOCK rules."""

import pytest


class TestRuleEngine:
    """Tests for the RuleEngine itself."""

    def test_register_and_check_pass(self, rule_engine, normal_adult_slots):
        result = rule_engine.check(normal_adult_slots)
        assert result.verdict == "PASS"
        assert len(result.triggered_rules) == 0

    def test_short_circuit_on_block(self, rule_engine):
        """首个 BLOCK 规则触发后应立即短路线返回，不再评估后续规则。"""
        slots = {
            "symptoms": [{"name": "发热"}],
            "temperature": 39.5,
            "duration_days": 4,
            "age": 8,
            "allergies": [],
            "other_symptoms": [],
            "medications_taken": [],
            "special_population": None,
            "chronic_conditions": [],
        }
        result = rule_engine.check(slots)
        assert result.verdict == "BLOCK"
        # 应该只有一个 BLOCK 规则触发（R1），后续规则被短路
        assert len(result.triggered_rules) == 1
        assert result.triggered_rules[0]["rule_id"] == "R1"


class TestR1HighFever:
    def test_trigger(self, rule_engine, high_fever_slots):
        result = rule_engine.check(high_fever_slots)
        assert result.verdict == "BLOCK"
        assert any(r["rule_id"] == "R1" for r in result.triggered_rules)

    def test_not_trigger_below_threshold(self, rule_engine, empty_slots):
        slots = {**empty_slots, "temperature": 38.9, "duration_days": 4}
        result = rule_engine.check(slots)
        assert result.verdict == "PASS"

    def test_not_trigger_short_duration(self, rule_engine, empty_slots):
        slots = {**empty_slots, "temperature": 39.5, "duration_days": 2}
        result = rule_engine.check(slots)
        assert result.verdict == "PASS"

    def test_not_trigger_no_temp(self, rule_engine, empty_slots):
        slots = {**empty_slots, "temperature": None, "duration_days": 4}
        result = rule_engine.check(slots)
        assert result.verdict == "PASS"


class TestR2InfantFever:
    def test_trigger(self, rule_engine, empty_slots):
        slots = {**empty_slots, "age": 0.1, "temperature": 38.0}
        result = rule_engine.check(slots)
        assert result.verdict == "BLOCK"
        assert any(r["rule_id"] == "R2" for r in result.triggered_rules)

    def test_not_trigger_older_baby(self, rule_engine, empty_slots):
        slots = {**empty_slots, "age": 0.5, "temperature": 38.0}
        result = rule_engine.check(slots)
        assert result.verdict == "PASS"

    def test_not_trigger_if_no_fever(self, rule_engine, empty_slots):
        slots = {**empty_slots, "age": 0.1, "temperature": None}
        result = rule_engine.check(slots)
        assert result.verdict == "PASS"


class TestR3PregnantFever:
    def test_trigger(self, rule_engine, pregnant_fever_slots):
        result = rule_engine.check(pregnant_fever_slots)
        assert result.verdict == "BLOCK"
        assert any(r["rule_id"] == "R3" for r in result.triggered_rules)

    def test_not_trigger_pregnant_no_fever(self, rule_engine, empty_slots):
        slots = {**empty_slots, "special_population": "pregnant", "temperature": None}
        result = rule_engine.check(slots)
        assert result.verdict == "PASS"

    def test_not_trigger_breastfeeding(self, rule_engine, empty_slots):
        slots = {
            **empty_slots,
            "special_population": "breastfeeding",
            "temperature": 39.0,
        }
        result = rule_engine.check(slots)
        assert "R3" not in [r["rule_id"] for r in result.triggered_rules]


class TestR4EmergencySigns:
    def test_trigger_breathing(self, rule_engine, emergency_slots):
        result = rule_engine.check(emergency_slots)
        assert result.verdict == "BLOCK"
        assert any(r["rule_id"] == "R4" for r in result.triggered_rules)

    def test_trigger_chest_pain(self, rule_engine, empty_slots):
        slots = {**empty_slots, "other_symptoms": ["胸痛"]}
        result = rule_engine.check(slots)
        assert result.verdict == "BLOCK"

    def test_not_trigger_normal_symptoms(self, rule_engine, empty_slots):
        slots = {**empty_slots, "other_symptoms": ["头痛", "流鼻涕"]}
        result = rule_engine.check(slots)
        assert result.verdict == "PASS"


class TestR5SevereAllergy:
    def test_trigger(self, rule_engine, empty_slots):
        slots = {**empty_slots, "other_symptoms": ["全身皮疹"]}
        result = rule_engine.check(slots)
        assert result.verdict == "BLOCK"
        assert any(r["rule_id"] == "R5" for r in result.triggered_rules)


class TestRulePluginArchitecture:
    """Test that rules are truly pluggable (spec N3)."""

    def test_add_new_rule_without_modifying_engine(self):
        from app.rules.base import RuleResult, SafetyRule
        from app.rules.engine import RuleEngine

        class MockNewRule(SafetyRule):
            rule_id = "TEST_NEW"
            description = "A new test rule"

            def evaluate(self, slots: dict) -> RuleResult:
                if slots.get("test_flag"):
                    return RuleResult(
                        triggered=True, action="BLOCK",
                        reason="Test rule triggered",
                    )
                return RuleResult()

        engine = RuleEngine()
        engine.register(MockNewRule())
        # Should pass
        result = engine.check({"test_flag": False})
        assert result.verdict == "PASS"
        # Should block
        result = engine.check({"test_flag": True})
        assert result.verdict == "BLOCK"


class TestRuleEnginePerformance:
    """Test N1: safety check < 100ms."""

    def test_check_performance(self, rule_engine, normal_adult_slots):
        import time
        start = time.perf_counter()
        rule_engine.check(normal_adult_slots)
        elapsed_ms = (time.perf_counter() - start) * 1000
        assert elapsed_ms < 100, f"Safety check took {elapsed_ms:.2f}ms, expected < 100ms"
