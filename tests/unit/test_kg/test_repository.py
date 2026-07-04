"""Unit tests for DrugGraphRepository — mock Neo4jClient, verify query logic."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.kg.client import Neo4jClient
from app.kg.repository import ANCESTOR_DECAY, DrugGraphRepository
from app.kg.schemas import ContraindicationResult, DrugCandidate


class TestDrugGraphRepository:
    """Tests for DrugGraphRepository business logic.

    All tests mock Neo4jClient.run() to return pre-defined results.
    No actual Neo4j connection needed.
    """

    @pytest.fixture
    def mock_client(self):
        """Create a Neo4jClient mock that is_available() returns True."""
        c = MagicMock(spec=Neo4jClient)
        c.is_available.return_value = True
        c.run = AsyncMock()
        return c

    @pytest.fixture
    def repo(self, mock_client):
        """Create a DrugGraphRepository with mock client."""
        return DrugGraphRepository(mock_client)

    # ── find_candidates_by_symptoms ──

    @pytest.mark.asyncio
    async def test_find_candidates_returns_sorted(self, repo, mock_client):
        """Should return DrugCandidates sorted by adjusted score descending."""
        mock_client.run.return_value = [
            {"drug": "布洛芬", "coverage_score": 1.35, "matched_symptoms": ["头痛", "发热"], "match_details": [], "drug_total_treats": 2},
            {"drug": "对乙酰氨基酚", "coverage_score": 1.30, "matched_symptoms": ["头痛", "发热"], "match_details": [], "drug_total_treats": 2},
            {"drug": "板蓝根颗粒", "coverage_score": 0.42, "matched_symptoms": ["头痛"], "match_details": [], "drug_total_treats": 1},
        ]

        symptoms = [
            {"name": "头痛", "weight": 1.0},
            {"name": "发热", "weight": 0.5},
        ]
        result = await repo.find_candidates_by_symptoms(symptoms)

        assert len(result) == 3
        assert result[0].generic_name == "布洛芬"
        assert result[1].generic_name == "对乙酰氨基酚"
        assert result[2].generic_name == "板蓝根颗粒"
        # When drug_total_treats == matched_count, specificity=1.0, adjusted = coverage
        assert result[0].coverage_score == 1.35

    @pytest.mark.asyncio
    async def test_find_candidates_empty_when_unavailable(self, repo, mock_client):
        """Should return empty list when Neo4j is unavailable."""
        mock_client.is_available.return_value = False

        symptoms = [{"name": "头痛", "weight": 1.0}]
        result = await repo.find_candidates_by_symptoms(symptoms)

        assert result == []

    @pytest.mark.asyncio
    async def test_find_candidates_empty_when_no_symptoms(self, repo):
        """Should return empty list when symptoms list is empty."""
        result = await repo.find_candidates_by_symptoms([])
        assert result == []

    @pytest.mark.asyncio
    async def test_find_candidates_calculates_score_correctly(self, repo, mock_client):
        """Coverage score: strength × weight × decay. Adjusted adds specificity."""
        # Direct match (0-hop): strength=0.9, weight=1.0, decay=1.0 → 0.9
        # Ancestor match (1-hop): strength=0.8, weight=1.0, decay=0.7 → 0.56
        mock_client.run.return_value = [
            {
                "drug": "布洛芬",
                "coverage_score": 0.90,
                "matched_symptoms": ["偏头痛"],
                "match_details": [
                    {"symptom": "偏头痛", "strength": 0.9, "distance": 0, "decay": 1.0, "contribution": 0.90},
                ],
                "drug_total_treats": 1,
            },
            {
                "drug": "酚麻美敏",
                "coverage_score": 0.56,
                "matched_symptoms": ["偏头痛"],
                "match_details": [
                    {"symptom": "偏头痛", "strength": 0.8, "distance": 1, "decay": ANCESTOR_DECAY, "contribution": 0.56},
                ],
                "drug_total_treats": 1,
            },
        ]

        symptoms = [{"name": "偏头痛", "weight": 1.0}]
        result = await repo.find_candidates_by_symptoms(symptoms)

        assert result[0].generic_name == "布洛芬"
        assert result[0].coverage_score == pytest.approx(0.90)
        assert result[1].generic_name == "酚麻美敏"
        assert result[1].coverage_score == pytest.approx(0.56)

    @pytest.mark.asyncio
    async def test_find_candidates_with_category_filter(self, repo, mock_client):
        """Should include category filter when categories are provided."""
        mock_client.run.return_value = []

        symptoms = [{"name": "头痛", "weight": 1.0}]
        await repo.find_candidates_by_symptoms(symptoms, categories=["感冒退烧"])

        # Verify the query includes the category parameter
        call_args = mock_client.run.call_args
        assert call_args[0][1].get("categories") == ["感冒退烧"]

    @pytest.mark.asyncio
    async def test_find_candidates_query_failure_returns_empty(self, repo, mock_client):
        """Should return empty list on query exception."""
        mock_client.run.side_effect = Exception("Cypher error")

        symptoms = [{"name": "头痛", "weight": 1.0}]
        result = await repo.find_candidates_by_symptoms(symptoms)

        assert result == []

    # ── check_contraindications ──

    @pytest.mark.asyncio
    async def test_check_contraindications_all_three_dimensions(self, repo, mock_client):
        """Should detect contraindications in all three dimensions."""
        # Q2: conditions
        mock_client.run.side_effect = [
            [{"matched_condition": "胃溃疡"}],       # condition check
            [{"matched_population": "孕妇"}],          # population check
            [{"matched_allergen": "布洛芬"}],          # allergy check
        ]

        result = await repo.check_contraindications(
            drug_name="布洛芬",
            user_conditions=["胃溃疡", "哮喘"],
            special_population="孕妇",
            allergies=["布洛芬"],
        )

        assert result.has_contraindication is True
        assert "胃溃疡" in result.matched_conditions
        assert "孕妇" in result.matched_populations
        assert "布洛芬" in result.matched_allergens

    @pytest.mark.asyncio
    async def test_check_contraindications_clean(self, repo, mock_client):
        """Should return clean result when no contraindications found."""
        mock_client.run.side_effect = [
            [],  # no condition matches
            [],  # no population matches
            [],  # no allergen matches
        ]

        result = await repo.check_contraindications(
            drug_name="布洛芬",
            user_conditions=["胃溃疡"],
            special_population=None,
            allergies=["青霉素"],
        )

        assert result.has_contraindication is False

    @pytest.mark.asyncio
    async def test_check_contraindications_unavailable(self, repo, mock_client):
        """Should return safe default when Neo4j unavailable."""
        mock_client.is_available.return_value = False

        result = await repo.check_contraindications(
            drug_name="布洛芬",
            user_conditions=["胃溃疡"],
            special_population="孕妇",
            allergies=["布洛芬"],
        )

        assert result.drug_name == "布洛芬"
        assert result.has_contraindication is False

    # ── get_similar_drugs ──

    @pytest.mark.asyncio
    async def test_get_similar_drugs_returns_alternatives(self, repo, mock_client):
        """Should return alternative drug names."""
        mock_client.run.return_value = [
            {"alternative": "对乙酰氨基酚"},
            {"alternative": "酚麻美敏"},
        ]

        result = await repo.get_similar_drugs("布洛芬")

        assert result == ["对乙酰氨基酚", "酚麻美敏"]

    @pytest.mark.asyncio
    async def test_get_similar_drugs_empty(self, repo, mock_client):
        """Should return empty list when no alternatives exist."""
        mock_client.run.return_value = []

        result = await repo.get_similar_drugs("正柴胡饮颗粒")

        assert result == []

    @pytest.mark.asyncio
    async def test_get_similar_drugs_unavailable(self, repo, mock_client):
        """Should return empty list when Neo4j unavailable."""
        mock_client.is_available.return_value = False

        result = await repo.get_similar_drugs("布洛芬")

        assert result == []

    # ── get_drug_profile ──

    @pytest.mark.asyncio
    async def test_get_drug_profile_returns_full_data(self, repo, mock_client):
        """Should return all contraindication and ingredient data."""
        mock_client.run.return_value = [{
            "drug": "布洛芬",
            "contraindicated_conditions": ["胃溃疡", "哮喘"],
            "contraindicated_populations": ["孕妇", "哺乳期"],
            "ingredients": ["布洛芬"],
        }]

        result = await repo.get_drug_profile("布洛芬")

        assert result["drug"] == "布洛芬"
        assert "胃溃疡" in result["contraindicated_conditions"]
        assert "孕妇" in result["contraindicated_populations"]
        assert "布洛芬" in result["ingredients"]

    @pytest.mark.asyncio
    async def test_get_drug_profile_unavailable(self, repo, mock_client):
        """Should return defaults when Neo4j unavailable."""
        mock_client.is_available.return_value = False

        result = await repo.get_drug_profile("布洛芬")

        assert result["drug"] == "布洛芬"
        assert result["contraindicated_conditions"] == []
        assert result["ingredients"] == []

    # ── Decay Logic Validation ──

    @pytest.mark.asyncio
    async def test_direct_match_no_decay(self, repo, mock_client):
        """Direct match (distance=0) should have decay=1.0."""
        mock_client.run.return_value = [
            {
                "drug": "布洛芬",
                "coverage_score": 0.9,
                "matched_symptoms": ["头痛"],
                "match_details": [
                    {"symptom": "头痛", "strength": 0.9, "distance": 0, "decay": 1.0, "contribution": 0.9},
                ],
                "drug_total_treats": 1,
            },
        ]

        result = await repo.find_candidates_by_symptoms([{"name": "头痛", "weight": 1.0}])

        assert len(result[0].match_details) == 1
        assert result[0].match_details[0].distance == 0
        assert result[0].match_details[0].decay == 1.0
        assert result[0].match_details[0].contribution == 0.9

    @pytest.mark.asyncio
    async def test_ancestor_match_with_decay(self, repo, mock_client):
        """Ancestor match (distance>0) should have decay=0.7."""
        mock_client.run.return_value = [
            {
                "drug": "酚麻美敏",
                "coverage_score": 0.504,
                "matched_symptoms": ["太阳穴跳痛"],
                "match_details": [
                    {"symptom": "太阳穴跳痛", "strength": 0.8, "distance": 2, "decay": ANCESTOR_DECAY, "contribution": 0.56},
                ],
                "drug_total_treats": 1,
            },
        ]

        result = await repo.find_candidates_by_symptoms([{"name": "太阳穴跳痛", "weight": 1.0}])

        assert result[0].match_details[0].distance == 2
        assert result[0].match_details[0].decay == ANCESTOR_DECAY
