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
from app.scorer.engine import (
    _normalize_weights,
    normalize_for_display,
    score_all,
    score_one,
    score_one_v2,
)
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


# ═══════════════════════════════════════════════════════════════
# Test 5: v2 Scoring Engine — hierarchical multiplicative model
# ═══════════════════════════════════════════════════════════════

class TestScoringEngineV2Diag:
    """Manual verification of the v2 hierarchical multiplicative formula."""

    def test_v2_specialist_drug_score(self):
        """Single symptom, specialist drug: manual calculation verification.

        formula: sm × focus^α × age^β × otc^γ
        右美沙芬: 0.57 × 0.25^0.5 × 1.0^0.3 × 0.7^0.05
                = 0.57 × 0.50 × 1.0 × 0.982 = 0.280
        """
        features = {
            "symptom_match": 0.57,
            "symptom_focus_ratio": 0.25,  # 1/4, specialist
            "age_suitability": 1.0,
            "otc_safety_level": 0.7,
        }
        exponents = {"focus": 0.5, "age": 0.3, "otc": 0.05}

        result = score_one_v2(features, exponents, 1, "右美沙芬")

        print(f"\n=== V2 Specialist Score: {result.total_score} ===")
        for dim in result.dimensions:
            print(f"  [{dim.feature_name}] exponent={dim.weight:.2f}, fv={dim.feature_value:.3f}, factor={dim.contribution}")

        # Manual: 0.57 * 0.5 * 1.0 * 0.982 = 0.280
        expected_sm = 0.57
        expected_focus_factor = 0.25 ** 0.5  # 0.5
        expected_age_factor = 1.0 ** 0.3  # 1.0
        expected_otc_factor = 0.7 ** 0.05  # ≈ 0.9823
        expected = round(expected_sm * expected_focus_factor * expected_age_factor * expected_otc_factor, 4)

        assert result.total_score == pytest.approx(expected)
        assert 0.25 < result.total_score < 0.35
        assert not result.excluded

    def test_v2_broad_spectrum_drug_score(self):
        """Single symptom, broad drug: should score significantly lower than specialist.

        formula: 酚麻美敏: 0.35 × 0.14^0.5 × 1.0^0.3 × 0.7^0.05
                = 0.35 × 0.374 × 1.0 × 0.982 = 0.129
        """
        features = {
            "symptom_match": 0.35,
            "symptom_focus_ratio": 0.14,  # 1/7, broad spectrum
            "age_suitability": 1.0,
            "otc_safety_level": 0.7,
        }
        exponents = {"focus": 0.5, "age": 0.3, "otc": 0.05}

        result = score_one_v2(features, exponents, 2, "酚麻美敏")

        print(f"\n=== V2 Broad Drug Score: {result.total_score} ===")

        expected_sm = 0.35
        expected_focus_factor = 0.14 ** 0.5  # ≈ 0.374
        expected = round(expected_sm * expected_focus_factor * 1.0 * (0.7 ** 0.05), 4)

        assert result.total_score == pytest.approx(expected)
        assert 0.10 < result.total_score < 0.18

    def test_v2_specialist_vs_broad_gap(self):
        """Specialist should score > 2x broad drug for single symptom."""
        features_specialist = {"symptom_match": 0.57, "symptom_focus_ratio": 0.25, "age_suitability": 1.0, "otc_safety_level": 0.7}
        features_broad = {"symptom_match": 0.35, "symptom_focus_ratio": 0.14, "age_suitability": 1.0, "otc_safety_level": 0.7}
        exponents = {"focus": 0.5, "age": 0.3, "otc": 0.05}

        specialist = score_one_v2(features_specialist, exponents, 1, "specialist")
        broad = score_one_v2(features_broad, exponents, 2, "broad")

        print(f"\n=== V2 Gap: specialist={specialist.total_score:.4f}, broad={broad.total_score:.4f} ===")
        print(f"  Ratio: {specialist.total_score / broad.total_score:.2f}x")

        assert specialist.total_score > broad.total_score * 2.0, (
            f"Specialist ({specialist.total_score:.4f}) should be > 2x broad ({broad.total_score:.4f})"
        )

    def test_v2_multi_symptom_broad_can_win(self):
        """Multi-symptom: broad drug with high sm + high focus can beat specialist with low sm."""
        # 3 symptoms match: broad drug matches all 3, focus high
        features_broad_multi = {"symptom_match": 0.80, "symptom_focus_ratio": 0.43, "age_suitability": 1.0, "otc_safety_level": 0.7}  # 3/7
        features_specialist_single = {"symptom_match": 0.57, "symptom_focus_ratio": 0.25, "age_suitability": 1.0, "otc_safety_level": 0.7}  # 1/4
        exponents = {"focus": 0.5, "age": 0.3, "otc": 0.05}

        broad = score_one_v2(features_broad_multi, exponents, 1, "broad_multi")
        specialist = score_one_v2(features_specialist_single, exponents, 2, "specialist_single")

        print(f"\n=== V2 Multi-symptom: broad={broad.total_score:.4f}, specialist={specialist.total_score:.4f} ===")

        # Broad with 3 matching symptoms should beat specialist with 1
        assert broad.total_score > specialist.total_score, (
            f"Multi-symptom broad ({broad.total_score:.4f}) should beat single-symptom specialist ({specialist.total_score:.4f})"
        )

    def test_v2_age_penalty_for_child(self):
        """Child using adult drug: age_suitability=0.4 → 0.4^0.3 = 0.76."""
        features = {"symptom_match": 0.57, "symptom_focus_ratio": 0.25, "age_suitability": 0.4, "otc_safety_level": 0.7}
        exponents = {"focus": 0.5, "age": 0.3, "otc": 0.05}

        result = score_one_v2(features, exponents, 1, "child_drug")

        print(f"\n=== V2 Child Age Penalty: {result.total_score:.4f} ===")

        expected_age_factor = 0.4 ** 0.3
        print(f"  age_factor: {expected_age_factor:.4f} (0.4^{0.3})")
        assert 0.55 < expected_age_factor < 0.80, f"Age penalty should be moderate, got {expected_age_factor:.3f}"

    def test_v2_otc_tiebreaker_only(self):
        """OTC should have near-zero impact: 乙类(1.0) vs 甲类(0.7) barely differs."""
        features_a = {"symptom_match": 0.57, "symptom_focus_ratio": 0.25, "age_suitability": 1.0, "otc_safety_level": 1.0}  # 乙类
        features_b = {"symptom_match": 0.57, "symptom_focus_ratio": 0.25, "age_suitability": 1.0, "otc_safety_level": 0.7}  # 甲类
        exponents = {"focus": 0.5, "age": 0.3, "otc": 0.05}

        score_a = score_one_v2(features_a, exponents, 1, "otc_b")
        score_b = score_one_v2(features_b, exponents, 2, "otc_a")

        gap = abs(score_a.total_score - score_b.total_score)
        ratio = score_a.total_score / score_b.total_score

        print(f"\n=== V2 OTC Tiebreaker: 乙类={score_a.total_score:.4f}, 甲类={score_b.total_score:.4f}, gap={gap:.4f}, ratio={ratio:.4f} ===")

        # Gap should be very small (< 5%)
        assert gap < 0.02, f"OTC gap should be < 0.02, got {gap:.4f}"

    def test_v2_score_clamped_to_one(self):
        """Score should be capped at 1.0."""
        features = {"symptom_match": 1.0, "symptom_focus_ratio": 1.0, "age_suitability": 1.0, "otc_safety_level": 1.0}
        exponents = {"focus": 0.5, "age": 0.3, "otc": 0.05}

        result = score_one_v2(features, exponents, 1, "perfect")

        assert result.total_score <= 1.0
        assert result.total_score == pytest.approx(1.0)

    def test_v2_missing_features_default_neutral(self):
        """Missing features should default to 1.0 (neutral, no penalty)."""
        features = {"symptom_match": 0.57}  # focus, age, otc all missing
        exponents = {"focus": 0.5, "age": 0.3, "otc": 0.05}

        result = score_one_v2(features, exponents, 1, "minimal")

        # All missing = 1.0 neutral, so score = sm × 1.0 × 1.0 × 1.0 = sm
        assert result.total_score == pytest.approx(0.57)

    def test_v2_version_dispatch(self):
        """score_one with scoring_version='v2' should use v2 formula."""
        features = {"symptom_match": 0.57, "symptom_focus_ratio": 0.25, "age_suitability": 1.0, "otc_safety_level": 0.7}
        exponents = {"focus": 0.5, "age": 0.3, "otc": 0.05}

        v2_result = score_one(features, exponents, 1, "test", scoring_version="v2")
        v2_direct = score_one_v2(features, exponents, 1, "test")

        assert v2_result.total_score == v2_direct.total_score
        assert v2_result.total_score < 0.5  # v2 score is lower than v1


# ═══════════════════════════════════════════════════════════════
# Test 6: Display normalization
# ═══════════════════════════════════════════════════════════════

class TestNormalizeForDisplay:
    """Tests for normalize_for_display() — sigmoid confidence calibration.

    Sigmoid parameters: k=12, midpoint=0.18
    Formula: display = 100 / (1 + exp(-12 * (score - 0.18)))
    """

    def test_sigmoid_high_confidence(self):
        """Excellent match (~0.49) → 97±2 (very high confidence)."""
        sd = make_scored_drug(1, "A", 0.49)
        result = normalize_for_display([sd])
        # sigmoid(12 * 0.31) = 100/(1+e^-3.72) ≈ 97.6
        assert 95 < result[0].display_score <= 100

    def test_sigmoid_specialist_drug(self):
        """Cough specialist (0.28) → 77±3 (good confidence)."""
        sd = make_scored_drug(1, "右美沙芬", 0.28)
        result = normalize_for_display([sd])
        # sigmoid(12 * 0.10) = 100/(1+e^-1.20) ≈ 76.9
        assert 73 < result[0].display_score < 81

    def test_sigmoid_broad_spectrum(self):
        """Broad drug (0.13) → 35±5 (moderate-low confidence)."""
        sd = make_scored_drug(1, "酚麻美敏", 0.13)
        result = normalize_for_display([sd])
        # sigmoid(12 * -0.05) = 100/(1+e^0.60) ≈ 35.4
        assert 30 < result[0].display_score < 41

    def test_sigmoid_poor_match(self):
        """Poor match (0.04) → ~16 (low confidence, not zero)."""
        sd = make_scored_drug(1, "poor", 0.04)
        result = normalize_for_display([sd])
        # sigmoid(12 * -0.14) = 100/(1+e^1.68) ≈ 15.7
        assert 10 < result[0].display_score < 22

    def test_sigmoid_batch_independent(self):
        """Same raw score → same display_score regardless of batch."""
        sd1 = make_scored_drug(1, "A", 0.28)
        sd2 = make_scored_drug(2, "B", 0.99)  # excellent drug in same batch
        result = normalize_for_display([sd1, sd2])
        # A's score should be ~77 regardless of B being in the batch
        assert 73 < result[0].display_score < 81

    def test_sigmoid_monotonic(self):
        """Higher raw score → higher display_score (monotonicity)."""
        sd1 = make_scored_drug(1, "A", 0.28)
        sd2 = make_scored_drug(2, "B", 0.13)
        sd3 = make_scored_drug(3, "C", 0.10)
        result = normalize_for_display([sd1, sd2, sd3])
        assert result[0].display_score > result[1].display_score > result[2].display_score

    def test_no_drug_gets_100_for_moderate_match(self):
        """A moderate match should not show as 100 — that's dishonest."""
        sd = make_scored_drug(1, "A", 0.28)  # good but not perfect
        result = normalize_for_display([sd])
        assert result[0].display_score < 90, (
            f"Moderate match shouldn't inflate to {result[0].display_score}"
        )

    def test_excluded_gets_zero(self):
        """Excluded drugs should get display_score = 0."""
        sd1 = make_scored_drug(1, "A", 0.57)
        sd2 = make_scored_drug(2, "B", 0.0, excluded=True)
        result = normalize_for_display([sd1, sd2])
        assert result[1].display_score == 0.0

    def test_all_excluded(self):
        """All excluded → all display_score = 0."""
        sd1 = make_scored_drug(1, "A", 0.0, excluded=True)
        sd2 = make_scored_drug(2, "B", 0.0, excluded=True)
        result = normalize_for_display([sd1, sd2])
        assert result[0].display_score == 0.0

    def test_perfect_score_reaches_high(self):
        """A theoretically perfect match (1.0) → 100."""
        sd = make_scored_drug(1, "perfect", 1.0)
        result = normalize_for_display([sd])
        assert result[0].display_score >= 99.9


def make_scored_drug(drug_id, name, score, excluded=False):
    """Helper to create a ScoredDrug for display normalization tests."""
    from app.scorer.schemas import ScoredDrug
    return ScoredDrug(
        drug_id=drug_id,
        generic_name=name,
        total_score=score,
        excluded=excluded,
        exclude_reason="test" if excluded else "",
    )
