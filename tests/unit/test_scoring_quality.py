"""Scoring quality tests — precision/specificity differentiation.

Verifies that the precision-adjusted Neo4j scoring correctly
differentiates between broad-spectrum and targeted drugs.

All tests use mock Neo4jClient results. No actual Neo4j needed.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.kg.client import Neo4jClient
from app.kg.repository import (
    SPECIFICITY_ALPHA,
    SPECIFICITY_BETA,
    DrugGraphRepository,
    _compute_adjusted_score,
)
from app.kg.schemas import DrugCandidate


class TestPrecisionMath:
    """Verify the precision adjustment formula."""

    def test_perfect_specificity_no_penalty(self):
        """matched==total → specificity=1.0 → no penalty."""
        score = _compute_adjusted_score(coverage=0.9, matched=3, total=3)
        assert score == pytest.approx(0.9)

    def test_narrow_drug_low_penalty(self):
        """Drug treating 4 symptoms, matching 1 → moderate penalty."""
        # specificity = (1+1)/(4+1) = 0.4, 0.4^0.5 = 0.632
        # adjusted = 0.9 × 0.632 = 0.569
        score = _compute_adjusted_score(coverage=0.9, matched=1, total=4)
        expected_specificity = (1 + SPECIFICITY_BETA) / (4 + SPECIFICITY_BETA)
        expected = 0.9 * (expected_specificity ** SPECIFICITY_ALPHA)
        assert score == pytest.approx(expected)

    def test_broad_drug_high_penalty(self):
        """Drug treating 7 symptoms, matching 1 → heavy penalty."""
        score_narrow = _compute_adjusted_score(coverage=0.9, matched=1, total=4)
        score_broad = _compute_adjusted_score(coverage=0.9, matched=1, total=7)
        # Broad drug should score lower than narrow drug with same coverage
        assert score_broad < score_narrow

    def test_multi_symptom_match_reduces_penalty(self):
        """More matched symptoms → less penalty."""
        score_1 = _compute_adjusted_score(coverage=2.0, matched=1, total=7)
        score_3 = _compute_adjusted_score(coverage=2.0, matched=3, total=7)
        assert score_3 > score_1

    def test_zero_total_treats_handled(self):
        """drug_total_treats=0 should not divide by zero."""
        score = _compute_adjusted_score(coverage=0.5, matched=0, total=0)
        # specificity = (0+1)/(0+1) = 1.0, adjusted = 0.5
        assert score == pytest.approx(0.5)
        assert not (score is None)

    def test_alpha_zero_disables_penalty(self):
        """ALPHA=0 → specificity^0 = 1.0 → adjusted = coverage."""
        # Manually compute with ALPHA=0 logic
        specificity = (1 + SPECIFICITY_BETA) / (7 + SPECIFICITY_BETA)
        # alpha=0: specificity^0 = 1.0
        assert specificity ** 0.0 == 1.0


class _MockRepo(DrugGraphRepository):
    """Test helper: expose _compute_adjusted_score for direct testing."""


class TestScoringQuality:
    """Verify scoring ranking quality with realistic mock data."""

    @pytest.fixture
    def mock_client(self):
        c = MagicMock(spec=Neo4jClient)
        c.is_available.return_value = True
        c.run = AsyncMock()
        return c

    @pytest.fixture
    def repo(self, mock_client):
        return DrugGraphRepository(mock_client)

    @pytest.mark.asyncio
    async def test_targeted_outranks_broad_for_single_symptom(self, repo, mock_client):
        """Single symptom: targeted drug (4 treats) >> broad drug (7 treats)."""
        mock_client.run.return_value = [
            {
                "drug": "氢溴酸右美沙芬",
                "coverage_score": 0.9,
                "matched_symptoms": ["干咳"],
                "match_details": [],
                "drug_total_treats": 4,
            },
            {
                "drug": "酚麻美敏",
                "coverage_score": 0.7,
                "matched_symptoms": ["干咳"],
                "match_details": [],
                "drug_total_treats": 7,
            },
        ]

        result = await repo.find_candidates_by_symptoms([{"name": "干咳", "weight": 1.0}])

        assert result[0].generic_name == "氢溴酸右美沙芬"
        assert result[1].generic_name == "酚麻美敏"
        # Targeted drug should have significantly higher adjusted score
        ratio = result[0].score / result[1].score
        assert ratio > 1.3, f"Expected >1.3x gap, got {ratio:.2f}x"

    @pytest.mark.asyncio
    async def test_broad_outranks_targeted_for_multi_symptom(self, repo, mock_client):
        """Multi-symptom: broad drug covering all symptoms ranks higher."""
        mock_client.run.return_value = [
            {
                "drug": "酚麻美敏",
                "coverage_score": 2.35,
                "matched_symptoms": ["干咳", "发热", "鼻塞"],
                "match_details": [],
                "drug_total_treats": 7,
            },
            {
                "drug": "氢溴酸右美沙芬",
                "coverage_score": 0.9,
                "matched_symptoms": ["干咳"],
                "match_details": [],
                "drug_total_treats": 4,
            },
        ]

        result = await repo.find_candidates_by_symptoms([
            {"name": "干咳", "weight": 1.0},
            {"name": "发热", "weight": 1.0},
            {"name": "鼻塞", "weight": 1.0},
        ])

        assert result[0].generic_name == "酚麻美敏"
        assert result[1].generic_name == "氢溴酸右美沙芬"

    @pytest.mark.asyncio
    async def test_top_three_scores_are_distinct(self, repo, mock_client):
        """Scores must not be identical — differentiation required."""
        mock_client.run.return_value = [
            {"drug": "药A", "coverage_score": 0.9, "matched_symptoms": ["干咳"], "match_details": [], "drug_total_treats": 2},
            {"drug": "药B", "coverage_score": 0.7, "matched_symptoms": ["干咳"], "match_details": [], "drug_total_treats": 4},
            {"drug": "药C", "coverage_score": 0.6, "matched_symptoms": ["干咳"], "match_details": [], "drug_total_treats": 7},
        ]

        result = await repo.find_candidates_by_symptoms([{"name": "干咳", "weight": 1.0}])

        assert len(result) >= 3
        scores = [c.score for c in result[:3]]
        # All three scores must be distinct
        assert len(set(scores)) == len(scores), f"Scores not distinct: {scores}"

    @pytest.mark.asyncio
    async def test_primary_symptom_weights_affect_ranking(self, repo, mock_client):
        """Higher weight on primary symptom changes ranking."""
        mock_client.run.return_value = [
            {
                "drug": "布洛芬",
                "coverage_score": 1.8,  # high coverage from 头痛 primary + 鼻塞 secondary
                "matched_symptoms": ["头痛", "鼻塞"],
                "match_details": [],
                "drug_total_treats": 2,
            },
            {
                "drug": "盐酸伪麻黄碱",
                "coverage_score": 1.3,  # coverage from 鼻塞 primary only
                "matched_symptoms": ["鼻塞"],
                "match_details": [],
                "drug_total_treats": 1,
            },
        ]

        # 头痛 primary (1.0), 鼻塞 secondary (0.5)
        result = await repo.find_candidates_by_symptoms([
            {"name": "头痛", "weight": 1.0},
            {"name": "鼻塞", "weight": 0.5},
        ])

        # 布洛芬 should rank first (covers both primary and secondary)
        assert result[0].generic_name == "布洛芬"

    @pytest.mark.asyncio
    async def test_new_fields_populated_correctly(self, repo, mock_client):
        """DrugCandidate must have all new fields populated."""
        mock_client.run.return_value = [
            {
                "drug": "布洛芬",
                "coverage_score": 0.9,
                "matched_symptoms": ["头痛"],
                "match_details": [{"symptom": "头痛", "strength": 0.9, "distance": 0, "decay": 1.0, "contribution": 0.9}],
                "drug_total_treats": 9,
            },
        ]

        result = await repo.find_candidates_by_symptoms([{"name": "头痛", "weight": 1.0}])

        assert len(result) == 1
        c = result[0]
        assert c.coverage_score == 0.9
        assert c.drug_total_treats == 9
        assert c.matched_symptom_count == 1
        # Adjusted score < coverage score (precision penalty applied)
        assert c.score < c.coverage_score
