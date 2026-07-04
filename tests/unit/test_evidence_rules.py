"""Unit tests for GraphRelevanceScore evidence rule."""

from unittest.mock import MagicMock

import pytest

from app.scorer.evidence.graph_relevance import GraphRelevanceScore
from app.scorer.schemas import EvidenceResult


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

    def test_merge_strategy_is_max(self, rule, mock_drug):
        """Should use max merge (compete with ILIKE, higher wins)."""
        mock_drug._graph_score = 0.57
        result = rule.evaluate({}, mock_drug)
        assert result.merge_strategy == "max"

    def test_uses_graph_score_when_available(self, rule, mock_drug):
        """When _graph_score is set, return it as feature value."""
        mock_drug._graph_score = 0.57
        result = rule.evaluate({}, mock_drug)

        assert isinstance(result, EvidenceResult)
        assert result.value == 0.57
        assert "图谱相关性" in result.reason

    def test_returns_zero_when_no_graph_score(self, rule, mock_drug):
        """When _graph_score is None, return 0.0 (ILIKE takes over)."""
        mock_drug._graph_score = None
        result = rule.evaluate({}, mock_drug)

        assert result.value == 0.0
        assert "降级" in result.reason

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
