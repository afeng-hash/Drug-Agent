"""Unit tests for evidence rules: GraphRelevanceScore and SymptomFocusRatio."""

from unittest.mock import MagicMock

import pytest

from app.scorer.evidence.graph_relevance import GraphRelevanceScore
from app.scorer.evidence.symptom_focus import SymptomFocusRatio
from app.scorer.schemas import EvidenceResult


# ═══════════════════════════════════════════════════════════════
# GraphRelevanceScore
# ═══════════════════════════════════════════════════════════════

class TestGraphRelevanceScore:
    """Tests for the GraphRelevanceScore evidence rule."""

    @pytest.fixture
    def rule(self):
        return GraphRelevanceScore()

    @pytest.fixture
    def mock_drug(self):
        """Create a mock Drug ORM object."""
        drug = MagicMock()
        drug._graph_score = None
        return drug

    def test_feature_name_is_symptom_match(self, rule):
        """Should target symptom_match dimension."""
        assert rule.feature_name == "symptom_match"

    def test_merge_strategy_is_set(self, rule, mock_drug):
        """Should use set merge (sole source for symptom_match)."""
        mock_drug._graph_score = 0.57
        result = rule.evaluate({}, mock_drug)
        assert result.merge_strategy == "set"

    def test_uses_graph_score_when_available(self, rule, mock_drug):
        """When _graph_score is set, return it as feature value."""
        mock_drug._graph_score = 0.57
        result = rule.evaluate({}, mock_drug)

        assert isinstance(result, EvidenceResult)
        assert result.value == 0.57
        assert "Graph relevance" in result.reason

    def test_returns_neutral_when_no_graph_score(self, rule, mock_drug):
        """When _graph_score is None, return 0.5 (neutral in geometric mean)."""
        mock_drug._graph_score = None
        result = rule.evaluate({}, mock_drug)

        assert result.value == 0.5
        assert "KG data unavailable" in result.reason

    def test_score_clamped_to_one(self, rule, mock_drug):
        """Score above 1.0 should be clamped (feature convention is 0-1)."""
        mock_drug._graph_score = 1.66
        result = rule.evaluate({}, mock_drug)

        assert result.value == 1.0

    def test_score_clamped_to_zero(self, rule, mock_drug):
        """Negative score should be clamped to 0."""
        mock_drug._graph_score = -0.1
        result = rule.evaluate({}, mock_drug)

        assert result.value == 0.0

    def test_zero_score_passed_through(self, rule, mock_drug):
        """Score of exactly 0.0 should pass through."""
        mock_drug._graph_score = 0.0
        result = rule.evaluate({}, mock_drug)

        assert result.value == 0.0

    def test_feature_value_is_float(self, rule, mock_drug):
        """Feature value must be float type."""
        mock_drug._graph_score = 0.57
        result = rule.evaluate({}, mock_drug)
        assert isinstance(result.value, float)


# ═══════════════════════════════════════════════════════════════
# SymptomFocusRatio
# ═══════════════════════════════════════════════════════════════

class TestSymptomFocusRatio:
    """Tests for the SymptomFocusRatio evidence rule.

    Formula: matched / drug_total_treats (drug specialization, not user recall).
    """

    @pytest.fixture
    def rule(self):
        return SymptomFocusRatio()

    @pytest.fixture
    def mock_drug(self):
        """Create a mock Drug ORM object with KG transient attributes."""
        drug = MagicMock()
        drug._graph_matched_count = None
        drug._graph_total_treats = None
        return drug

    def test_feature_name_is_focus_ratio(self, rule):
        """Should target symptom_focus_ratio dimension."""
        assert rule.feature_name == "symptom_focus_ratio"

    def test_specialist_drug_high_focus(self, rule, mock_drug):
        """Specialist drug (treats few, matches proportionally more) → high focus."""
        mock_drug._graph_matched_count = 1
        mock_drug._graph_total_treats = 4   # cough specialist, treats 4 symptoms
        result = rule.evaluate({}, mock_drug)

        assert result.value == 0.25
        assert "聚焦率=0.25" in result.reason

    def test_broad_spectrum_drug_low_focus(self, rule, mock_drug):
        """Broad-spectrum drug (treats many, matches few) → low focus."""
        mock_drug._graph_matched_count = 1
        mock_drug._graph_total_treats = 10  # cold panacea, treats 10 symptoms
        result = rule.evaluate({}, mock_drug)

        assert result.value == 0.1
        assert "聚焦率=0.10" in result.reason

    def test_all_symptoms_matched_max_focus(self, rule, mock_drug):
        """When drug only treats exactly the matched symptoms → focus=1.0."""
        mock_drug._graph_matched_count = 3
        mock_drug._graph_total_treats = 3   # perfect specialist
        result = rule.evaluate({}, mock_drug)

        assert result.value == 1.0
        assert "极高聚焦" in result.reason

    def test_no_matched_symptoms_returns_zero(self, rule, mock_drug):
        """When drug matches 0 user symptoms, return 0.0."""
        mock_drug._graph_matched_count = 0
        mock_drug._graph_total_treats = 5
        result = rule.evaluate({}, mock_drug)

        assert result.value == 0.0
        assert "未匹配" in result.reason

    def test_kg_matched_count_unavailable_neutral(self, rule, mock_drug):
        """When _graph_matched_count is None, return 1.0 (neutral)."""
        mock_drug._graph_matched_count = None
        mock_drug._graph_total_treats = 5
        result = rule.evaluate({}, mock_drug)

        assert result.value == 1.0
        assert "KG数据不可用" in result.reason

    def test_kg_total_treats_unavailable_neutral(self, rule, mock_drug):
        """When _graph_total_treats is None, return 1.0 (neutral)."""
        mock_drug._graph_matched_count = 3
        mock_drug._graph_total_treats = None
        result = rule.evaluate({}, mock_drug)

        assert result.value == 1.0
        assert "KG数据不可用" in result.reason

    def test_kg_total_treats_zero_neutral(self, rule, mock_drug):
        """When _graph_total_treats is 0 (edge case), return 1.0 (neutral)."""
        mock_drug._graph_matched_count = 1
        mock_drug._graph_total_treats = 0
        result = rule.evaluate({}, mock_drug)

        assert result.value == 1.0
        assert "KG数据不可用" in result.reason

    def test_ratio_capped_at_one(self, rule, mock_drug):
        """Ratio should not exceed 1.0 even if matched > total (data anomaly)."""
        mock_drug._graph_matched_count = 5
        mock_drug._graph_total_treats = 3
        result = rule.evaluate({}, mock_drug)

        assert result.value == 1.0
