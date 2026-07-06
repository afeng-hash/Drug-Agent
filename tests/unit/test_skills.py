"""
Unit tests for ReactAgent Skills infrastructure (Phase 1).

Tests SOPEngine, SkillRouter, and data sufficiency logic.
TaskClassifier and ResponseGenerator tested via integration (mock LLM).
"""

import pytest

from app.agent.react.skills.types import (
    SOP,
    SOPResult,
    SOPStep,
    StepResult,
    TaskType,
)
from app.agent.react.skills.sop import SOPEngine, _has_usable_data, _fill_template
from app.agent.react.skills.router import SkillRouter
from app.agent.react.skills.task_definitions import (
    TASK_SOP_MAP,
    ALL_TASK_DEFINITIONS,
    SIDE_EFFECTS_SOP,
    DRUG_INTERACTION_SOP,
    DRUG_COMPARISON_SOP,
    RECOMMENDATION_EXPLANATION_SOP,
    SPECIAL_POPULATION_SOP,
)


# =====================================================================
# Data Sufficiency Tests
# =====================================================================


class TestHasUsableData:
    """_has_usable_data() — content presence detection."""

    def test_empty_list_is_not_usable(self):
        assert _has_usable_data([[]]) is False

    def test_empty_dict_is_not_usable(self):
        assert _has_usable_data([{}]) is False

    def test_search_manual_with_sufficient_content(self):
        data = [{"content": "This is a long enough text that exceeds fifty characters minimum threshold for the test to pass successfully."}]
        assert _has_usable_data([data]) is True

    def test_search_manual_short_content_is_not_usable(self):
        data = [{"content": "short"}]
        assert _has_usable_data([data]) is False

    def test_multiple_chunks_total_exceeds_threshold(self):
        data = [
            {"content": "short1"},
            {"content": "short2"},
            {"content": "this is a much longer content string that pushes the total over the threshold limit"},
        ]
        assert _has_usable_data([data]) is True

    def test_get_drug_detail_with_meaningful_field(self):
        data = {"adverse_reactions": "This field has enough characters to pass the minimum threshold."}
        assert _has_usable_data([data]) is True

    def test_get_drug_detail_only_metadata_is_not_usable(self):
        data = {"drug_id": 1, "generic_name": "ibuprofen", "trade_names": "Advil", "category": "NSAID"}
        assert _has_usable_data([data]) is False

    def test_get_drug_detail_short_field_is_not_usable(self):
        data = {"adverse_reactions": "N/A"}
        assert _has_usable_data([data]) is False

    def test_error_dict_is_not_usable(self):
        data = {"error": "Drug not found"}
        assert _has_usable_data([data]) is False

    def test_empty_dict_marker_is_not_usable(self):
        data = {"empty": True, "message": "No data"}
        assert _has_usable_data([data]) is False

    def test_found_false_is_not_usable(self):
        data = {"found": False, "results": []}
        assert _has_usable_data([data]) is False

    def test_web_search_with_results_is_usable(self):
        data = {"source": "web", "results": [{"title": "Test", "snippet": "Content about the drug"}]}
        assert _has_usable_data([data]) is True

    def test_web_search_empty_results_is_not_usable(self):
        data = {"source": "web", "results": []}
        assert _has_usable_data([data]) is False

    def test_combined_local_and_db_data(self):
        manual_data = [{"content": "Some content from search_manual"}]
        db_data = {"generic_name": "ibuprofen", "adverse_reactions": "This is a meaningful field with enough text"}
        assert _has_usable_data([manual_data, db_data]) is True

    def test_none_is_skipped(self):
        assert _has_usable_data([None, [], {}]) is False


# =====================================================================
# Template Filling Tests
# =====================================================================


class TestFillTemplate:
    """_fill_template() — placeholder substitution."""

    def test_simple_drug_name_substitution(self):
        result = _fill_template(
            {"drug_name": "{drug_name}", "question": "side effects"},
            {"drug_name": "ibuprofen"},
        )
        assert result == {"drug_name": "ibuprofen", "question": "side effects"}

    def test_population_substitution(self):
        result = _fill_template(
            {"drug_name": "{drug_name}", "question": "{population} safety"},
            {"drug_name": "acetaminophen", "population": "pregnant"},
        )
        assert result == {"drug_name": "acetaminophen", "question": "pregnant safety"}

    def test_numeric_string_conversion(self):
        result = _fill_template(
            {"top_k": "5", "drug_name": "{drug_name}"},
            {"drug_name": "ibuprofen"},
        )
        assert result == {"top_k": 5, "drug_name": "ibuprofen"}

    def test_unmatched_placeholder_remains(self):
        result = _fill_template(
            {"drug_name": "{drug_name}", "question": "{custom_focus}"},
            {"drug_name": "ibuprofen"},
        )
        assert result == {"drug_name": "ibuprofen", "question": "{custom_focus}"}


# =====================================================================
# SOPEngine Tests
# =====================================================================


class TestSOPEngineGrouping:
    """SOPEngine._group_by_parallel() step grouping logic."""

    def test_sequential_steps(self):
        steps = [
            SOPStep(order=1, tool_name="search_manual", args_template={"drug_name": "{drug_name}"}),
            SOPStep(order=2, tool_name="get_drug_detail", args_template={"drug_name": "{drug_name}"}),
            SOPStep(order=3, tool_name="search_web", args_template={"query": "test"}),
        ]
        engine = SOPEngine(tool_registry=None)
        groups = engine._group_by_parallel(steps)
        assert len(groups) == 3  # each step is its own group

    def test_parallel_group(self):
        steps = [
            SOPStep(order=1, tool_name="search_manual", args_template={"drug_name": "{drug_a}"}, parallel_group=1),
            SOPStep(order=1, tool_name="search_manual", args_template={"drug_name": "{drug_b}"}, parallel_group=1),
            SOPStep(order=2, tool_name="search_web", args_template={"query": "test"}),
        ]
        engine = SOPEngine(tool_registry=None)
        groups = engine._group_by_parallel(steps)
        assert len(groups) == 2  # group 1 (2 parallel) + group 2 (1 sequential)


# =====================================================================
# SkillRouter Tests
# =====================================================================


class TestSkillRouter:
    """SkillRouter.route() deterministic routing."""

    def test_compare_drugs_routes_directly(self):
        router = SkillRouter()
        result = router.route(intent="compare_drugs", query="which is better")
        assert result == TaskType.DRUG_COMPARISON

    def test_ask_interaction_routes_directly(self):
        router = SkillRouter()
        result = router.route(intent="ask_interaction", query="can i take both")
        assert result == TaskType.DRUG_INTERACTION

    def test_ask_drug_returns_none_for_classification(self):
        router = SkillRouter()
        result = router.route(intent="ask_drug", query="what are the side effects")
        assert result is None

    def test_chat_returns_none_for_fallback(self):
        router = SkillRouter()
        result = router.route(intent="chat", query="hello")
        assert result is None

    def test_recommendation_explanation_with_context(self):
        router = SkillRouter()
        result = router.route(
            intent="ask_drug",
            query="为什么推荐布洛芬",
            has_recommendations=True,
        )
        assert result == TaskType.RECOMMENDATION_EXPLANATION

    def test_recommendation_explanation_not_recommend(self):
        router = SkillRouter()
        result = router.route(
            intent="ask_drug",
            query="为什么不推荐对乙酰氨基酚",
            has_recommendations=True,
        )
        assert result == TaskType.RECOMMENDATION_EXPLANATION

    def test_recommendation_explanation_without_context_no_match(self):
        router = SkillRouter()
        result = router.route(
            intent="ask_drug",
            query="为什么推荐布洛芬",
            has_recommendations=False,
        )
        assert result is None  # no recommendations -> not detected


# =====================================================================
# SOP Definitions Tests
# =====================================================================


class TestSOPDefinitions:
    """All 8 SOP definitions are valid and complete."""

    def test_all_eight_sops_defined(self):
        assert len(TASK_SOP_MAP) == 8
        assert len(ALL_TASK_DEFINITIONS) == 8

    def test_each_sop_has_steps(self):
        for sop in ALL_TASK_DEFINITIONS:
            assert len(sop.steps) > 0, f"{sop.task_type} has no steps"

    def test_search_web_is_last_step(self):
        for sop in ALL_TASK_DEFINITIONS:
            web_steps = [s for s in sop.steps if s.tool_name == "search_web"]
            if web_steps:
                assert web_steps[-1].order == max(s.order for s in sop.steps), (
                    f"{sop.task_type}: search_web should be the last step"
                )

    def test_side_effects_has_three_steps(self):
        assert len(SIDE_EFFECTS_SOP.steps) == 3

    def test_drug_interaction_has_parallel_group(self):
        parallel_steps = [s for s in DRUG_INTERACTION_SOP.steps if s.parallel_group > 0]
        assert len(parallel_steps) == 2

    def test_drug_comparison_has_parallel_group(self):
        parallel_steps = [s for s in DRUG_COMPARISON_SOP.steps if s.parallel_group > 0]
        assert len(parallel_steps) == 4

    def test_recommendation_explanation_has_state_tools(self):
        step_names = [s.tool_name for s in RECOMMENDATION_EXPLANATION_SOP.steps]
        assert "get_recommendation" in step_names
        assert "get_user_profile" in step_names

    def test_special_population_has_population_param(self):
        step1 = SPECIAL_POPULATION_SOP.steps[0]
        assert "{population}" in step1.args_template.get("question", "")

    def test_all_have_fallback_response(self):
        for sop in ALL_TASK_DEFINITIONS:
            assert sop.fallback_response, f"{sop.task_type} has no fallback_response"

    def test_all_have_mandatory_reminders(self):
        for sop in ALL_TASK_DEFINITIONS:
            assert len(sop.mandatory_reminders) > 0, f"{sop.task_type} has no reminders"
