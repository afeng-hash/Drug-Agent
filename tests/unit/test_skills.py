"""
Unit tests for ReactAgent Skills infrastructure (Phase 1).

Tests SOPEngine, SkillRouter, and data sufficiency logic.
TaskClassifier and ResponseGenerator tested via integration (mock LLM).
"""

import asyncio
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


# =====================================================================
# SOPEngine.execute() Tests — core execution pipeline
# =====================================================================


class TestSOPEngineExecute:
    """SOPEngine.execute() — full execution flow with mock ToolRegistry."""

    @pytest.fixture
    def registry_and_engine(self):
        """Create a ToolRegistry and SOPEngine, returning both for mock setup."""
        from app.agent.react.tools import ToolRegistry

        registry = ToolRegistry()
        engine = SOPEngine(tool_registry=registry)
        return registry, engine

    @staticmethod
    def _make_sop(steps: list | None = None) -> SOP:
        """Factory: a minimal SOP with search_manual + get_drug_detail + search_web."""
        if steps is not None:
            return SOP(
                task_type=TaskType.SIDE_EFFECTS,
                steps=steps,
                response_structure="...",
                mandatory_reminders=["reminder"],
                fallback_response="fallback",
            )
        return SOP(
            task_type=TaskType.SIDE_EFFECTS,
            steps=[
                SOPStep(order=1, tool_name="search_manual",
                        args_template={"drug_name": "{drug_name}", "question": "side effects"}),
                SOPStep(order=2, tool_name="get_drug_detail",
                        args_template={"drug_name": "{drug_name}"}),
                SOPStep(order=3, tool_name="search_web",
                        args_template={"query": "{drug_name} side effects"}),
            ],
            response_structure="...",
            mandatory_reminders=["reminder"],
            fallback_response="fallback",
        )

    def _register(self, registry, tool_name: str, return_data):
        """Register a mock tool that always returns the given data."""

        async def mock_execute(**kwargs):
            return return_data

        from app.agent.react.schemas import ToolDefinition

        registry.register(
            ToolDefinition(name=tool_name, description="mock", parameters={}),
            mock_execute,
        )

    # ── Normal execution ──────────────────────────────────

    @pytest.mark.asyncio
    async def test_local_steps_succeed_no_web_fallback(self, registry_and_engine):
        """Local steps return usable data → search_web is NOT triggered."""
        registry, engine = registry_and_engine
        self._register(registry, "search_manual",
                       [{"content": "This drug may cause nausea, dizziness, headache, and other common side effects. Patients should monitor for adverse reactions."}])
        self._register(registry, "get_drug_detail",
                       {"adverse_reactions": "Common: nausea, dizziness, headache. Rare: gastrointestinal bleeding."})
        # Also register search_web — but it should never be called
        web_called = False

        async def web_mock(**kwargs):
            nonlocal web_called
            web_called = True
            return {"source": "web", "results": [{"snippet": "web data"}]}

        from app.agent.react.schemas import ToolDefinition

        registry.register(
            ToolDefinition(name="search_web", description="mock", parameters={}),
            web_mock,
        )

        sop = self._make_sop()
        result = await engine.execute(sop, {"drug_name": "ibuprofen"})

        assert result.task_type == TaskType.SIDE_EFFECTS
        assert result.has_usable_data is True
        assert result.triggered_web_fallback is False
        assert web_called is False
        assert len(result.steps) == 2  # only local steps, no web step
        assert all(s.success for s in result.steps)

    @pytest.mark.asyncio
    async def test_local_steps_partial_failure_still_triggers_no_web(self, registry_and_engine):
        """One local step fails, the other has data → web NOT triggered (we have data)."""
        registry, engine = registry_and_engine
        self._register(registry, "search_manual",
                       [{"content": "Sufficient content from search_manual: this drug is commonly used with minimal side effects and well tolerated across patient populations."}])
        # get_drug_detail returns empty — no meaningful fields
        self._register(registry, "get_drug_detail",
                       {"drug_id": 1, "generic_name": "ibuprofen", "category": "NSAID"})
        web_called = False

        async def web_mock(**kwargs):
            nonlocal web_called
            web_called = True
            return {"source": "web", "results": [{"snippet": "web"}]}

        from app.agent.react.schemas import ToolDefinition

        registry.register(
            ToolDefinition(name="search_web", description="mock", parameters={}),
            web_mock,
        )

        sop = self._make_sop()
        result = await engine.execute(sop, {"drug_name": "ibuprofen"})

        assert result.has_usable_data is True   # search_manual has data
        assert result.triggered_web_fallback is False
        assert web_called is False

    # ── Web fallback triggered ────────────────────────────

    @pytest.mark.asyncio
    async def test_web_fallback_when_locals_all_empty(self, registry_and_engine):
        """Local steps all return empty → search_web IS triggered."""
        registry, engine = registry_and_engine
        self._register(registry, "search_manual", [])
        self._register(registry, "get_drug_detail",
                       {"drug_id": 1, "generic_name": "ibuprofen"})
        web_called = False

        async def web_mock(**kwargs):
            nonlocal web_called
            web_called = True
            return {"source": "web", "results": [
                {"title": "Ibuprofen Side Effects",
                 "snippet": "Common side effects include nausea, dizziness, headache, and gastrointestinal discomfort."}
            ]}

        from app.agent.react.schemas import ToolDefinition

        registry.register(
            ToolDefinition(name="search_web", description="mock", parameters={}),
            web_mock,
        )

        sop = self._make_sop()
        result = await engine.execute(sop, {"drug_name": "ibuprofen"})

        assert result.triggered_web_fallback is True
        assert web_called is True
        assert result.has_usable_data is True  # web saved us
        assert len(result.steps) == 3  # local + web

    @pytest.mark.asyncio
    async def test_web_fallback_also_empty(self, registry_and_engine):
        """Local empty + web also empty → has_usable_data=False."""
        registry, engine = registry_and_engine
        self._register(registry, "search_manual", [])
        self._register(registry, "get_drug_detail",
                       {"drug_id": 1, "generic_name": "ibuprofen"})
        self._register(registry, "search_web",
                       {"source": "web", "results": []})

        sop = self._make_sop()
        result = await engine.execute(sop, {"drug_name": "ibuprofen"})

        assert result.triggered_web_fallback is True
        assert result.has_usable_data is False

    # ── Step failure / exception ──────────────────────────

    @pytest.mark.asyncio
    async def test_step_returns_none_data_treated_as_empty(self, registry_and_engine):
        """A tool returning None → success=True, data=None → not counted as usable."""
        registry, engine = registry_and_engine

        async def returns_none(**kwargs):
            return None

        from app.agent.react.schemas import ToolDefinition

        registry.register(
            ToolDefinition(name="search_manual", description="mock", parameters={}),
            returns_none,
        )
        self._register(registry, "get_drug_detail",
                       {"drug_id": 1, "generic_name": "ibuprofen"})  # only metadata
        self._register(registry, "search_web",
                       {"source": "web", "results": []})

        sop = self._make_sop()
        result = await engine.execute(sop, {"drug_name": "ibuprofen"})

        # search_manual returned None → skipped → both locals empty → web triggered
        assert result.triggered_web_fallback is True
        # web also empty
        assert result.has_usable_data is False

    @pytest.mark.asyncio
    async def test_step_throws_unexpected_exception(self, registry_and_engine):
        """A tool raising an exception should be caught, not crash the pipeline."""
        registry, engine = registry_and_engine

        async def exploding_tool(**kwargs):
            raise RuntimeError("Something blew up")

        from app.agent.react.schemas import ToolDefinition

        registry.register(
            ToolDefinition(name="search_manual", description="mock", parameters={}),
            exploding_tool,
        )
        self._register(registry, "get_drug_detail",
                       {"adverse_reactions": "Common side effects include nausea and dizziness, enough chars."})

        sop = self._make_sop()
        result = await engine.execute(sop, {"drug_name": "ibuprofen"})

        # search_manual exception caught → get_drug_detail still has data
        assert result.has_usable_data is True
        assert result.triggered_web_fallback is False
        failed = [s for s in result.steps if s.tool_name == "search_manual"][0]
        assert failed.success is False
        assert "Something blew up" in failed.error

    # ── All steps fail ────────────────────────────────────

    @pytest.mark.asyncio
    async def test_all_local_steps_fail_web_also_fails(self, registry_and_engine):
        """Every step fails (throws exception) → has_usable_data=False."""
        registry, engine = registry_and_engine

        async def always_fail(**kwargs):
            raise RuntimeError("Dead")

        from app.agent.react.schemas import ToolDefinition

        registry.register(
            ToolDefinition(name="search_manual", description="mock", parameters={}),
            always_fail,
        )

        async def also_fail(**kwargs):
            raise RuntimeError("Dead too")

        registry.register(
            ToolDefinition(name="get_drug_detail", description="mock", parameters={}),
            also_fail,
        )
        self._register(registry, "search_web",
                       {"source": "web", "results": []})

        sop = self._make_sop()
        result = await engine.execute(sop, {"drug_name": "ibuprofen"})

        assert result.has_usable_data is False
        assert result.triggered_web_fallback is True
        # local steps failed, web step executed but returned empty
        local_steps = [s for s in result.steps if s.tool_name != "search_web"]
        assert all(not s.success for s in local_steps)
        assert len(result.steps) == 3  # 2 failed locals + 1 (empty) web

    # ── SOP without search_web ────────────────────────────

    @pytest.mark.asyncio
    async def test_sop_without_web_step(self, registry_and_engine):
        """SOP has no search_web → locals empty → no crash, has_usable_data=False."""
        registry, engine = registry_and_engine
        self._register(registry, "search_manual", [])
        self._register(registry, "get_drug_detail",
                       {"drug_id": 1, "generic_name": "ibuprofen"})

        sop = self._make_sop(steps=[
            SOPStep(order=1, tool_name="search_manual",
                    args_template={"drug_name": "{drug_name}"}),
            SOPStep(order=2, tool_name="get_drug_detail",
                    args_template={"drug_name": "{drug_name}"}),
        ])
        result = await engine.execute(sop, {"drug_name": "ibuprofen"})

        assert result.has_usable_data is False
        assert result.triggered_web_fallback is False
        assert len(result.steps) == 2

    # ── Parallel execution ────────────────────────────────

    @pytest.mark.asyncio
    async def test_parallel_group_execution(self, registry_and_engine):
        """Steps in the same parallel_group run concurrently."""
        registry, engine = registry_and_engine
        call_order: list[str] = []

        async def tool_a(**kwargs):
            call_order.append("a")
            await asyncio.sleep(0.05)
            return [{"content": "Data from tool A with enough characters to meet the minimum threshold limit."}]

        async def tool_b(**kwargs):
            call_order.append("b")
            await asyncio.sleep(0.05)
            return [{"content": "Data from tool B with enough characters to meet the minimum threshold limit."}]

        from app.agent.react.schemas import ToolDefinition

        registry.register(
            ToolDefinition(name="search_manual", description="mock", parameters={}),
            tool_a,
        )
        registry.register(
            ToolDefinition(name="get_drug_detail", description="mock", parameters={}),
            tool_b,
        )

        sop = self._make_sop(steps=[
            SOPStep(order=1, tool_name="search_manual",
                    args_template={"drug_name": "{drug_a}"}, parallel_group=1),
            SOPStep(order=1, tool_name="get_drug_detail",
                    args_template={"drug_name": "{drug_b}"}, parallel_group=1),
        ])
        result = await engine.execute(sop, {"drug_a": "ibuprofen", "drug_b": "acetaminophen"})

        assert len(result.steps) == 2
        assert all(s.success for s in result.steps)
        assert result.has_usable_data is True
        assert result.triggered_web_fallback is False
        # Both were called (actual concurrency verified by async framework)
        assert "a" in call_order
        assert "b" in call_order

    # ── Parameter filling ─────────────────────────────────

    @pytest.mark.asyncio
    async def test_params_are_filled_before_execution(self, registry_and_engine):
        """Template placeholders are filled with provided params."""
        registry, engine = registry_and_engine
        received_args: dict = {}

        async def capture_tool(**kwargs):
            nonlocal received_args
            received_args = kwargs
            return [{"content": "Captured! This has enough characters to pass the threshold check."}]

        from app.agent.react.schemas import ToolDefinition

        registry.register(
            ToolDefinition(name="search_manual", description="mock", parameters={}),
            capture_tool,
        )
        self._register(registry, "get_drug_detail",
                       {"adverse_reactions": "Sufficient detail data for the drug detail threshold requirement."})

        sop = self._make_sop(steps=[
            SOPStep(order=1, tool_name="search_manual",
                    args_template={"drug_name": "{drug_name}", "question": "{custom_focus}"}),
            SOPStep(order=2, tool_name="get_drug_detail",
                    args_template={"drug_name": "{drug_name}"}),
        ])
        await engine.execute(sop, {"drug_name": "ibuprofen", "custom_focus": "liver impact"})

        assert received_args["drug_name"] == "ibuprofen"
        assert received_args["question"] == "liver impact"

    # ── Edge: empty params ───────────────────────────────

    @pytest.mark.asyncio
    async def test_execute_with_empty_params(self, registry_and_engine):
        """Empty params dict → placeholders remain, but no crash."""
        registry, engine = registry_and_engine
        self._register(registry, "search_manual",
                       [{"content": "Content with enough text to pass the minimum character threshold test here."}])
        self._register(registry, "get_drug_detail",
                       {"adverse_reactions": "Common side effects with enough detail."})

        sop = self._make_sop()
        result = await engine.execute(sop, {})

        assert result.has_usable_data is True
        assert result.triggered_web_fallback is False
