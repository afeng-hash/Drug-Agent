"""
End-to-end diagnostic test for the ScoringPipeline.

Traces every step from weight loading → evidence evaluation → scoring,
printing all intermediate values to pinpoint where scores go to zero.

Run:  python -m pytest tests/unit/test_scoring_pipeline_diag.py -v -s
"""

import asyncio
import math
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db.models import WeightConfig
from app.db.repositories.weight_config import WeightConfigRepository
from app.scorer.engine import score_all, score_one, _normalize_weights
from app.scorer.evidence_engine import EvidenceEngine, DEFAULT_FEATURES
from app.scorer.evidence import (
    AgeSuitability,
    GraphRelevanceScore,
    OtcSafetyLevel,
    SymptomFocusRatio,
)
from app.scorer.pipeline import ScoringPipeline
from app.scorer.schemas import ScoringResult, ScoredDrug


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def make_mock_drug(
    drug_id: int,
    generic_name: str,
    otc_type: str = "甲类",
    graph_score: float | None = None,
    graph_matched_count: int | None = None,
    graph_total_treats: int | None = None,
    usage_adult: str = "一次1粒，一日2次",
    usage_child: str | None = None,
    usage_elderly: str | None = None,
) -> MagicMock:
    """Create a mock Drug ORM object with all required attributes."""
    drug = MagicMock()
    drug.id = drug_id
    drug.generic_name = generic_name
    drug.otc_type = otc_type
    drug.usage_adult = usage_adult
    drug.usage_child = usage_child
    drug.usage_elderly = usage_elderly
    drug.indication_summary = f"用于缓解{generic_name}相关症状"
    drug.active_ingredients = [generic_name]
    drug._graph_score = graph_score
    drug._graph_matched_count = graph_matched_count
    drug._graph_total_treats = graph_total_treats
    return drug


def make_mock_weight_config(
    weights: dict[str, float] | None = None,
    policy: str = "balanced",
    safety_threshold: float = 0.2,
    version: str = "v2.0.0",
) -> WeightConfig:
    """Create a mock WeightConfig ORM object."""
    if weights is None:
        weights = {
            "symptom_match": 0.50,
            "symptom_focus_ratio": 0.15,
            "age_suitability": 0.25,
            "otc_safety_level": 0.10,
        }
    config = MagicMock(spec=WeightConfig)
    config.weights = weights
    config.policy = policy
    config.safety_block_threshold = safety_threshold
    config.version = version
    return config


def make_mock_weight_repo(config: WeightConfig) -> WeightConfigRepository:
    """Create a mock WeightConfigRepository that returns the given config."""
    repo = MagicMock(spec=WeightConfigRepository)
    repo.get_active = AsyncMock(return_value=config)
    return repo


# ═══════════════════════════════════════════════════════════════
# Test 1: Evidence Engine — feature vector generation
# ═══════════════════════════════════════════════════════════════

class TestEvidenceEngineDiag:
    """Diagnose EvidenceEngine feature vector generation."""

    def test_features_with_kg_data(self):
        """All 4 rules should produce correct feature values with KG data.

        focus_ratio = matched / drug_total_treats = 1/4 = 0.25 (cough specialist).
        """
        engine = EvidenceEngine()
        engine.register(GraphRelevanceScore())
        engine.register(SymptomFocusRatio())
        engine.register(AgeSuitability())
        engine.register(OtcSafetyLevel())

        drug = make_mock_drug(
            drug_id=1,
            generic_name="氢溴酸右美沙芬",
            otc_type="甲类",
            graph_score=0.57,
            graph_matched_count=1,
            graph_total_treats=4,   # cough specialist treats ~4 symptoms
        )

        slots = {
            "symptoms": [{"name": "干咳"}],
            "age": 30,
        }

        features, details = engine.evaluate_with_detail(slots, drug)

        print("\n=== Evidence Engine Output (with KG data) ===")
        print(f"Features: {features}")
        for d in details:
            print(f"  [{d.feature_name}] value={d.value:.3f}, strategy={d.merge_strategy}, reason={d.reason}")

        assert features["symptom_match"] == pytest.approx(0.57), f"symptom_match should be 0.57, got {features['symptom_match']}"
        assert features["symptom_focus_ratio"] == pytest.approx(0.25), f"focus_ratio should be 0.25 (1/4), got {features['symptom_focus_ratio']}"
        assert features["age_suitability"] == 1.0, f"age_suitability should be 1.0 (adult), got {features['age_suitability']}"
        assert features["otc_safety_level"] == 0.7, f"otc_safety_level should be 0.7 (jia), got {features['otc_safety_level']}"

    def test_features_without_kg_data(self):
        """Without _graph_score, symptom_match should be 0.0."""
        engine = EvidenceEngine()
        engine.register(GraphRelevanceScore())
        engine.register(SymptomFocusRatio())
        engine.register(AgeSuitability())
        engine.register(OtcSafetyLevel())

        drug = make_mock_drug(
            drug_id=1,
            generic_name="氢溴酸右美沙芬",
            otc_type="甲类",
            graph_score=None,  # <-- KG not available
            graph_matched_count=None,
        )

        slots = {
            "symptoms": [{"name": "干咳"}],
            "age": 30,
        }

        features, details = engine.evaluate_with_detail(slots, drug)

        print("\n=== Evidence Engine Output (WITHOUT KG data) ===")
        print(f"Features: {features}")
        for d in details:
            print(f"  [{d.feature_name}] value={d.value:.3f}, strategy={d.merge_strategy}, reason={d.reason}")

        # KG unavailable → GraphRelevanceScore returns 0.5 (neutral)
        assert features["symptom_match"] == 0.5, f"symptom_match should be 0.5 (neutral), got {features['symptom_match']}"
        assert features["symptom_focus_ratio"] == 1.0  # neutral when KG unavailable


# ═══════════════════════════════════════════════════════════════
# Test 2: Scoring Engine — geometric formula
# ═══════════════════════════════════════════════════════════════

class TestScoringEngineDiag:
    """Diagnose the geometric mean scoring formula."""

    def test_score_one_with_kg_data(self):
        """With valid KG features, score should be well above 0.

        focus_ratio=0.25 simulates a cough specialist (1/4 symptoms matched).
        """
        features = {
            "symptom_match": 0.57,
            "symptom_focus_ratio": 0.25,  # 1/4 — specialist drug
            "age_suitability": 1.0,
            "otc_safety_level": 0.7,
        }
        weights = {
            "symptom_match": 0.50,
            "symptom_focus_ratio": 0.15,
            "age_suitability": 0.25,
            "otc_safety_level": 0.10,
        }

        result = score_one(features, weights, drug_id=1, generic_name="右美沙芬")

        print("\n=== Score Output (with KG data) ===")
        print(f"Total score: {result.total_score}")
        print(f"Excluded: {result.excluded}")
        for dim in result.dimensions:
            print(f"  [{dim.feature_name}] w={dim.weight:.2f}, fv={dim.feature_value:.3f}, contrib(ln)={dim.contribution}")

        # Manual verification
        norm = _normalize_weights(weights)
        log_total = sum(norm[k] * math.log(max(features.get(k, 1.0), 1e-8)) for k in norm)
        expected = round(math.exp(log_total), 4)
        print(f"Expected (manual calc): {expected}")

        assert result.total_score > 0.5, f"Score should be > 0.5, got {result.total_score}"
        assert result.total_score == pytest.approx(expected)
        assert not result.excluded

    def test_score_one_with_neutral_symptom_match(self):
        """When symptom_match is neutral (0.5, KG unavailable), score is reasonable."""
        features = {
            "symptom_match": 0.5,        # <-- KG not available, neutral
            "symptom_focus_ratio": 1.0,
            "age_suitability": 1.0,
            "otc_safety_level": 0.7,
        }
        weights = {
            "symptom_match": 0.50,
            "symptom_focus_ratio": 0.15,
            "age_suitability": 0.25,
            "otc_safety_level": 0.10,
        }

        result = score_one(features, weights, drug_id=1, generic_name="youmeishafen")

        print("\n=== Score Output (WITHOUT KG data, neutral fallback) ===")
        print(f"Total score: {result.total_score}")
        for dim in result.dimensions:
            print(f"  [{dim.feature_name}] w={dim.weight:.2f}, fv={dim.feature_value:.3f}, contrib(ln)={dim.contribution}")

        # Manual verification
        norm = _normalize_weights(weights)
        log_total = sum(norm[k] * math.log(max(features.get(k, 1.0), 1e-8)) for k in norm)
        expected = round(math.exp(log_total), 4)
        print(f"Expected (manual calc): {expected}")

        # With symptom_match=0.5 (neutral), score should be ~0.68
        assert result.total_score > 0.5, f"Neutral symptom_match should give reasonable score, got {result.total_score}"

    def test_score_one_with_old_db_weights(self):
        """Old DB weights (6 dims) with new features (4 dims) should still work."""
        features = {
            "symptom_match": 0.57,
            "symptom_focus_ratio": 1.0,
            "age_suitability": 1.0,
            "otc_safety_level": 0.7,
        }
        # Old weights from DB (before migration)
        old_weights = {
            "symptom_match": 0.30,
            "safety": 0.25,
            "age_suitability": 0.20,
            "otc_safety_level": 0.10,
            "ingredient_coverage": 0.10,
            "evidence_quality": 0.05,
        }

        result = score_one(features, old_weights, drug_id=1, generic_name="右美沙芬")

        print("\n=== Score Output (OLD DB weights) ===")
        print(f"Total score: {result.total_score}")
        for dim in result.dimensions:
            print(f"  [{dim.feature_name}] w={dim.weight:.2f}, fv={dim.feature_value:.3f}, contrib(ln)={dim.contribution}")

        assert result.total_score > 0.5, (
            f"Old weights should still produce non-zero scores, got {result.total_score}"
        )


# ═══════════════════════════════════════════════════════════════
# Test 3: Full Pipeline — end to end
# ═══════════════════════════════════════════════════════════════

class TestFullPipelineDiag:
    """End-to-end pipeline test with all components connected."""

    @pytest.mark.asyncio
    async def test_pipeline_with_kg_data(self):
        """Full pipeline: weight loading → evidence → scoring → sorting.

        focus_ratio = matched/drug_total_treats:
          - 右美沙芬: 1/4=0.25 (cough specialist)
          - 酚麻美敏: 1/7≈0.14 (broad cold medicine)
          - 维C银翘片: 1/6≈0.17 (cold medicine)
        """
        pipeline = ScoringPipeline()

        # Create mock drugs WITH KG data (normal path)
        drugs = [
            make_mock_drug(1, "氢溴酸右美沙芬", "甲类", graph_score=0.57, graph_matched_count=1, graph_total_treats=4),
            make_mock_drug(2, "酚麻美敏", "甲类", graph_score=0.35, graph_matched_count=1, graph_total_treats=7),
            make_mock_drug(3, "维C银翘片", "甲类", graph_score=0.24, graph_matched_count=1, graph_total_treats=6),
        ]

        slots = {
            "symptoms": [{"name": "干咳"}],
            "age": 30,
        }

        config = make_mock_weight_config()
        repo = make_mock_weight_repo(config)

        result = await pipeline.run(
            candidates=drugs,
            slots=slots,
            session_id="test-session",
            weight_repo=repo,
        )

        print("\n=== Full Pipeline Output (KG data available) ===")
        print(f"Config version: {result.config_version}")
        print(f"Time: {result.total_time_ms}ms")
        for sd in result.drugs:
            print(f"\n  {sd.generic_name}: score={sd.total_score:.4f}, excluded={sd.excluded}")
            for dim in sd.dimensions:
                print(f"    [{dim.feature_name}] w={dim.weight:.2f}, fv={dim.feature_value:.3f}, contrib={dim.contribution}")

        # Verify
        assert len(result.drugs) == 3
        scores = [d.total_score for d in result.drugs]
        print(f"\n  Scores: {scores}")

        # All scores should be non-zero
        for sd in result.drugs:
            assert sd.total_score > 0.3, f"{sd.generic_name} score={sd.total_score} is too low!"
            assert not sd.excluded

        # Order should be: 右美沙芬 > 酚麻美敏 > 维C银翘片
        assert scores[0] > scores[1] > scores[2], f"Wrong order: {scores}"

        print("\n[OK] Pipeline works correctly with KG data!")

    @pytest.mark.asyncio
    async def test_pipeline_without_kg_data(self):
        """Pipeline WITHOUT KG data: symptom_match=0.5 neutral, scores are reasonable.

        GraphRelevanceScore now returns 0.5 (neutral) when _graph_score is None,
        preventing the geometric mean from being crushed.
        """
        pipeline = ScoringPipeline()

        # Create mock drugs WITHOUT KG data
        drugs = [
            make_mock_drug(1, "youmeishafen", "A", graph_score=None, graph_matched_count=None),
            make_mock_drug(2, "fenmameimin", "A", graph_score=None, graph_matched_count=None),
            make_mock_drug(3, "weiCyinqiao", "A", graph_score=None, graph_matched_count=None),
        ]

        slots = {
            "symptoms": [{"name": "cough"}],
            "age": 30,
        }

        config = make_mock_weight_config()
        repo = make_mock_weight_repo(config)

        result = await pipeline.run(
            candidates=drugs,
            slots=slots,
            session_id="test-session",
            weight_repo=repo,
        )

        print("\n=== Full Pipeline Output (WITHOUT KG data, neutral fallback) ===")
        print(f"Config version: {result.config_version}")
        for sd in result.drugs:
            print(f"\n  {sd.generic_name}: score={sd.total_score:.4f}, excluded={sd.excluded}")
            for dim in sd.dimensions:
                print(f"    [{dim.feature_name}] w={dim.weight:.2f}, fv={dim.feature_value:.3f}, contrib={dim.contribution}")

        scores = [d.total_score for d in result.drugs]
        print(f"\n  Scores: {scores}")

        # With neutral symptom_match=0.5, all drugs should have reasonable scores
        for sd in result.drugs:
            assert sd.total_score > 0.4, f"{sd.generic_name} score={sd.total_score} too low!"
            assert not sd.excluded

        print("\n[OK] Neutral fallback works correctly!")

    @pytest.mark.asyncio
    async def test_pipeline_with_old_db_weights(self):
        """Full pipeline with old DB weight config (6 dims → 4 features)."""
        pipeline = ScoringPipeline()

        drugs = [
            make_mock_drug(1, "氢溴酸右美沙芬", "甲类", graph_score=0.57, graph_matched_count=1, graph_total_treats=4),
            make_mock_drug(2, "酚麻美敏", "甲类", graph_score=0.35, graph_matched_count=1, graph_total_treats=7),
        ]

        slots = {"symptoms": [{"name": "干咳"}], "age": 30}

        # Old DB weights
        old_weights = {
            "symptom_match": 0.30,
            "safety": 0.25,
            "age_suitability": 0.20,
            "otc_safety_level": 0.10,
            "ingredient_coverage": 0.10,
            "evidence_quality": 0.05,
        }
        config = make_mock_weight_config(weights=old_weights)
        repo = make_mock_weight_repo(config)

        result = await pipeline.run(
            candidates=drugs,
            slots=slots,
            session_id="test-session",
            weight_repo=repo,
        )

        print("\n=== Full Pipeline Output (OLD DB weights) ===")
        for sd in result.drugs:
            print(f"\n  {sd.generic_name}: score={sd.total_score:.4f}, excluded={sd.excluded}")
            for dim in sd.dimensions:
                print(f"    [{dim.feature_name}] w={dim.weight:.2f}, fv={dim.feature_value:.3f}, contrib={dim.contribution}")

        for sd in result.drugs:
            assert sd.total_score > 0.3, f"{sd.generic_name}: score={sd.total_score} too low with old weights!"

        print("\n[OK] Old DB weights + new features work correctly!")


# ═══════════════════════════════════════════════════════════════
# Test 4: Weight normalization edge cases
# ═══════════════════════════════════════════════════════════════

class TestNormalizeWeightsDiag:
    """Diagnose weight normalization edge cases."""

    def test_already_normalized(self):
        """Weights summing to 1.0 should stay the same."""
        weights = {"symptom_match": 0.50, "age_suitability": 0.25, "otc_safety_level": 0.10, "symptom_focus_ratio": 0.15}
        result = _normalize_weights(weights)
        for k, v in weights.items():
            assert result[k] == pytest.approx(v)

    def test_raw_weights_normalized(self):
        """Raw integer weights should be normalized."""
        weights = {"symptom_match": 50, "age_suitability": 25, "otc_safety_level": 10, "symptom_focus_ratio": 15}
        result = _normalize_weights(weights)
        assert sum(result.values()) == pytest.approx(1.0)
        assert result["symptom_match"] == pytest.approx(0.50)

    def test_all_zeros(self):
        """All-zero weights should be returned as-is."""
        weights = {"a": 0.0, "b": 0.0}
        result = _normalize_weights(weights)
        assert result == weights
