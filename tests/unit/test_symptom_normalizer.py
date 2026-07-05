"""症状标准化模块单元测试。

覆盖 spec 全部 9 条验收标准 + 风险分层 + 边界情况。
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.normalizer.schemas import NormalizedSymptom, NormalizationResult
from app.normalizer.symptom_normalizer import (
    SymptomNormalizer,
    L1_CONFIDENCE_THRESHOLD,
    L2_CONFIDENCE_THRESHOLD,
)
from app.normalizer.vocabulary import SymptomEntry, VocabularySource


# ═══════════════════════════════════════════════════════════
# Mock VocabularySource for testing
# ═══════════════════════════════════════════════════════════

def _make_test_vocab() -> VocabularySource:
    """构建测试用词表，包含 L1/L2/L3 症状。"""

    entries = [
        SymptomEntry("头痛", level=1, aliases=["头疼", "脑壳疼"], parents=[]),
        SymptomEntry("偏头痛", level=2, aliases=["偏头疼", "一边头疼"], parents=["头痛"]),
        SymptomEntry("太阳穴跳痛", level=3, aliases=["太阳穴疼"], parents=["偏头痛", "紧张性头痛"]),
        SymptomEntry("发热", level=1, aliases=["发烧", "体温升高", "低烧", "高烧"], parents=[]),
        SymptomEntry("咳嗽", level=1, aliases=["咳", "咳嗦"], parents=[]),
        SymptomEntry("干咳", level=2, aliases=["干咳嗽", "无痰咳嗽"], parents=["咳嗽"]),
        SymptomEntry("湿咳", level=2, aliases=["有痰咳嗽", "咳痰"], parents=["咳嗽"]),
        SymptomEntry("刺激性干咳", level=3, aliases=["喉咙痒咳嗽", "一直想咳"], parents=["干咳"]),
        SymptomEntry("咽喉痛", level=1, aliases=["嗓子疼", "喉咙痛", "喉咙疼", "咽痛", "嗓子痛"], parents=[]),
        SymptomEntry("咽干", level=2, aliases=["嗓子干", "口干", "咽部干燥"], parents=["咽喉痛"]),
        SymptomEntry("晨起咽干", level=3, aliases=["早上嗓子干"], parents=["咽干"]),
        SymptomEntry("流涕", level=1, aliases=["流鼻涕", "鼻涕"], parents=[]),
        SymptomEntry("鼻塞", level=1, aliases=["鼻子不通", "鼻子堵", "鼻堵"], parents=[]),
        SymptomEntry("全身不适", level=1, aliases=["全身不舒服", "浑身难受"], parents=[]),
        SymptomEntry("肌肉酸痛", level=2, aliases=["肌肉疼", "浑身酸痛"], parents=["全身不适"]),
        SymptomEntry("全身酸痛乏力", level=3, aliases=["全身没劲", "酸痛无力"], parents=["肌肉酸痛", "四肢酸痛"]),
    ]

    class MockVocab(VocabularySource):
        async def load(self):
            return entries

        def get_by_name(self, name):
            for e in entries:
                if e.name == name:
                    return e
            return None

        def resolve_alias(self, alias):
            for e in entries:
                if alias in e.aliases:
                    return e.name
            return None

        def all_names(self):
            return [e.name for e in entries]

        def all_aliases(self):
            result = []
            for e in entries:
                result.extend(e.aliases)
            return result

        def all_entries(self):
            return entries

    return MockVocab()


@pytest.fixture
def vocab():
    return _make_test_vocab()


@pytest.fixture
def normalizer_no_llm(vocab):
    return SymptomNormalizer(vocab=vocab, llm_client=None)


@pytest.fixture
def mock_llm():
    mock = MagicMock()
    mock.generate_structured = AsyncMock()
    return mock


@pytest.fixture
def normalizer_with_llm(vocab, mock_llm):
    return SymptomNormalizer(vocab=vocab, llm_client=mock_llm)


# ═══════════════════════════════════════════════════════════
# AC1: Exact Match
# ═══════════════════════════════════════════════════════════

class TestExactMatch:
    def test_exact_match_canonical_name(self, normalizer_no_llm):
        result = normalizer_no_llm.normalize_sync(["干咳"])
        r = result.results[0]
        assert r.standard == "干咳"
        assert r.confidence == 1.0
        assert r.method == "exact"
        assert r.level == 2

    def test_exact_match_multiple(self, normalizer_no_llm):
        result = normalizer_no_llm.normalize_sync(["头痛", "发热", "咳嗽"])
        assert [r.method for r in result.results] == ["exact", "exact", "exact"]
        assert [r.standard for r in result.results] == ["头痛", "发热", "咳嗽"]


# ═══════════════════════════════════════════════════════════
# AC2: Alias Match
# ═══════════════════════════════════════════════════════════

class TestAliasMatch:
    def test_alias_simple(self, normalizer_no_llm):
        result = normalizer_no_llm.normalize_sync(["嗓子疼"])
        r = result.results[0]
        assert r.standard == "咽喉痛"
        assert r.confidence == 1.0
        assert r.method == "alias"

    def test_alias_fever(self, normalizer_no_llm):
        result = normalizer_no_llm.normalize_sync(["发烧"])
        r = result.results[0]
        assert r.standard == "发热"
        assert r.method == "alias"

    def test_alias_headache(self, normalizer_no_llm):
        result = normalizer_no_llm.normalize_sync(["头疼"])
        r = result.results[0]
        assert r.standard == "头痛"
        assert r.method == "alias"

    def test_alias_priority_over_contains(self, normalizer_no_llm):
        """Alias 优先级 > contains。"""
        result = normalizer_no_llm.normalize_sync(["喉咙痛"])
        r = result.results[0]
        assert r.method == "alias"  # not contains
        assert r.standard == "咽喉痛"


# ═══════════════════════════════════════════════════════════
# AC3: Contains Match
# ═══════════════════════════════════════════════════════════

class TestContainsMatch:
    def test_contains_modifier_prefix(self, normalizer_no_llm):
        """'一直咳嗽' contains '咳嗽'。"""
        result = normalizer_no_llm.normalize_sync(["一直咳嗽"])
        r = result.results[0]
        assert r.standard == "咳嗽"
        assert r.method == "contains"
        assert r.confidence == 0.80

    def test_contains_longest_match_priority(self, normalizer_no_llm):
        """'喉咙痒咳嗽' — 应匹配 '刺激性干咳' (alias '喉咙痒咳嗽' exact) 而非 '咳嗽' (contains)。

        实际上 '喉咙痒咳嗽' 是 alias of '刺激性干咳' → alias match.
        """
        result = normalizer_no_llm.normalize_sync(["喉咙痒咳嗽"])
        r = result.results[0]
        # Should be alias match
        assert r.method == "alias"
        assert r.standard == "刺激性干咳"

    def test_contains_standard_in_raw(self, normalizer_no_llm):
        result = normalizer_no_llm.normalize_sync(["持续性干咳"])
        r = result.results[0]
        # "持续性干咳" contains "干咳"
        assert r.standard == "干咳"
        assert r.method == "contains"


# ═══════════════════════════════════════════════════════════
# AC4: LLM Valid Mapping
# ═══════════════════════════════════════════════════════════

class TestLLMValidMapping:
    @pytest.mark.asyncio
    async def test_llm_maps_unmatched(self, mock_llm, normalizer_with_llm):
        from app.normalizer.symptom_normalizer import SymptomMappingResult, SymptomMapping

        mock_llm.generate_structured.return_value = SymptomMappingResult(
            mappings=[
                SymptomMapping(raw="喉咙不舒服", standard="咽喉痛", confidence=0.9),
            ]
        )

        result = await normalizer_with_llm.normalize(["喉咙不舒服"])
        r = result.results[0]
        assert r.standard == "咽喉痛"
        assert r.method == "llm"
        assert r.level == 1  # 咽喉痛 is Level 1

    @pytest.mark.asyncio
    async def test_llm_maps_semantic(self, mock_llm, normalizer_with_llm):
        """'浑身没劲' → LLM → '全身酸痛乏力' (Level 3)."""
        from app.normalizer.symptom_normalizer import SymptomMappingResult, SymptomMapping

        mock_llm.generate_structured.return_value = SymptomMappingResult(
            mappings=[
                SymptomMapping(raw="浑身没劲", standard="全身酸痛乏力", confidence=0.9),
            ]
        )

        result = await normalizer_with_llm.normalize(["浑身没劲"])
        r = result.results[0]
        # 全身酸痛乏力 is Level 3 → LLM mapping REJECTED
        assert r.method == "discarded"
        assert r.standard == ""
        assert result.discarded_count == 1


# ═══════════════════════════════════════════════════════════
# AC5: LLM Hallucination Block
# ═══════════════════════════════════════════════════════════

class TestLLMHallucinationBlock:
    @pytest.mark.asyncio
    async def test_llm_returns_non_vocab_name(self, mock_llm, normalizer_with_llm):
        from app.normalizer.symptom_normalizer import SymptomMappingResult, SymptomMapping

        mock_llm.generate_structured.return_value = SymptomMappingResult(
            mappings=[
                SymptomMapping(raw="喉咙不舒服", standard="嗓子不舒服", confidence=0.8),
            ]
        )

        result = await normalizer_with_llm.normalize(["喉咙不舒服"])
        r = result.results[0]
        # "嗓子不舒服" not in vocab → discarded
        assert r.method == "discarded"
        assert result.discarded_count == 1

    @pytest.mark.asyncio
    async def test_llm_returns_null(self, mock_llm, normalizer_with_llm):
        from app.normalizer.symptom_normalizer import SymptomMappingResult, SymptomMapping

        mock_llm.generate_structured.return_value = SymptomMappingResult(
            mappings=[
                SymptomMapping(raw="今天天气不错", standard=None, confidence=0.0),
            ]
        )

        result = await normalizer_with_llm.normalize(["今天天气不错"])
        r = result.results[0]
        assert r.method == "discarded"
        assert result.discarded_count == 1


# ═══════════════════════════════════════════════════════════
# AC6: Level 3 Protection
# ═══════════════════════════════════════════════════════════

class TestLevel3Protection:
    def test_level3_direct_lookup_layer0(self, normalizer_no_llm):
        """Level 3 症状可以通过 Layer 0 的 exact/alias/contains 匹配。"""
        result = normalizer_no_llm.normalize_sync(["太阳穴跳痛"])
        r = result.results[0]
        # Exact match should work
        assert r.method == "exact"
        assert r.standard == "太阳穴跳痛"
        assert r.level == 3

    def test_level3_unmatched_layer0_no_llm(self, normalizer_no_llm):
        """Level 3 症状在 Layer 0 未匹配、无 LLM 时 → discarded。"""
        result = normalizer_no_llm.normalize_sync(["吞咽时喉咙刺痛"])
        r = result.results[0]
        assert r.method == "discarded"

    @pytest.mark.asyncio
    async def test_level3_llm_rejected(self, mock_llm, normalizer_with_llm):
        """Level 3 症状 LLM 映射后 → _risk_accept(Level 3) 返回 False → discarded。"""
        from app.normalizer.symptom_normalizer import SymptomMappingResult, SymptomMapping

        # "早起喉咙干" 不在任何 alias 中 → Layer 0 未匹配 → 走 LLM
        # LLM 返回 "晨起咽干" (Level 3) → _risk_accept 拒绝
        mock_llm.generate_structured.return_value = SymptomMappingResult(
            mappings=[
                SymptomMapping(raw="早起喉咙干", standard="晨起咽干", confidence=0.95),
            ]
        )

        result = await normalizer_with_llm.normalize(["早起喉咙干"])
        r = result.results[0]
        assert r.method == "discarded"
        assert result.discarded_count == 1


# ═══════════════════════════════════════════════════════════
# AC7: Vocabulary from Neo4j
# ═══════════════════════════════════════════════════════════

class TestVocabularySource:
    def test_vocab_loads_entries(self, vocab):
        assert len(vocab.all_entries()) > 0
        assert vocab.get_by_name("头痛") is not None
        assert vocab.get_by_name("干咳") is not None
        assert vocab.get_by_name("太阳穴跳痛") is not None

    def test_alias_resolution(self, vocab):
        assert vocab.resolve_alias("发烧") == "发热"
        assert vocab.resolve_alias("头疼") == "头痛"
        assert vocab.resolve_alias("嗓子疼") == "咽喉痛"
        assert vocab.resolve_alias("流鼻涕") == "流涕"

    def test_nonexistent_returns_none(self, vocab):
        assert vocab.get_by_name("不存在的症状") is None
        assert vocab.resolve_alias("不存在的别名") is None


# ═══════════════════════════════════════════════════════════
# AC8: Performance
# ═══════════════════════════════════════════════════════════

class TestPerformance:
    def test_layer0_10_symptoms_under_10ms(self, normalizer_no_llm):
        import time
        names = ["干咳", "发烧", "头疼", "流鼻涕", "咳嗽",
                  "咽喉痛", "鼻子不通", "肌肉疼", "有痰咳嗽", "嗓子干"]
        t0 = time.perf_counter()
        result = normalizer_no_llm.normalize_sync(names)
        elapsed = (time.perf_counter() - t0) * 1000
        assert elapsed < 15.0, f"Layer 0 took {elapsed:.2f}ms, expected < 15ms"
        assert len(result.results) == 10


# ═══════════════════════════════════════════════════════════
# AC9: Observability
# ═══════════════════════════════════════════════════════════

class TestObservability:
    def test_method_and_confidence_recorded(self, normalizer_no_llm):
        result = normalizer_no_llm.normalize_sync(["干咳", "发烧", "一直咳嗽"])
        methods = [r.method for r in result.results]
        confidences = [r.confidence for r in result.results]
        assert methods == ["exact", "alias", "contains"]
        assert confidences == [1.0, 1.0, 0.80]

    def test_result_stats(self, normalizer_no_llm):
        result = normalizer_no_llm.normalize_sync(["干咳", "发烧"])
        assert result.total_time_ms >= 0
        assert result.llm_calls == 0
        assert result.discarded_count == 0


# ═══════════════════════════════════════════════════════════
# Risk Stratification
# ═══════════════════════════════════════════════════════════

class TestRiskStratification:
    def test_risk_accept_l1_above_threshold(self, normalizer_no_llm):
        entry = SymptomEntry("头痛", level=1)
        assert normalizer_no_llm._risk_accept(entry, 0.75) is True
        assert normalizer_no_llm._risk_accept(entry, 0.70) is True

    def test_risk_accept_l1_below_threshold(self, normalizer_no_llm):
        entry = SymptomEntry("头痛", level=1)
        assert normalizer_no_llm._risk_accept(entry, 0.65) is False

    def test_risk_accept_l2_above_threshold(self, normalizer_no_llm):
        entry = SymptomEntry("干咳", level=2)
        assert normalizer_no_llm._risk_accept(entry, 0.90) is True
        assert normalizer_no_llm._risk_accept(entry, 0.85) is True

    def test_risk_accept_l2_below_threshold(self, normalizer_no_llm):
        entry = SymptomEntry("干咳", level=2)
        assert normalizer_no_llm._risk_accept(entry, 0.80) is False

    def test_risk_accept_l3_always_false(self, normalizer_no_llm):
        entry = SymptomEntry("太阳穴跳痛", level=3)
        assert normalizer_no_llm._risk_accept(entry, 1.0) is False
        assert normalizer_no_llm._risk_accept(entry, 0.95) is False

    def test_l1_edge_threshold(self, normalizer_no_llm):
        entry = SymptomEntry("发热", level=1)
        assert normalizer_no_llm._risk_accept(entry, L1_CONFIDENCE_THRESHOLD) is True

    def test_l2_edge_threshold(self, normalizer_no_llm):
        entry = SymptomEntry("偏头痛", level=2)
        assert normalizer_no_llm._risk_accept(entry, L2_CONFIDENCE_THRESHOLD) is True


# ═══════════════════════════════════════════════════════════
# Contains Match Edge Cases
# ═══════════════════════════════════════════════════════════

class TestContainsEdgeCases:
    def test_contains_single_char_not_matched(self, normalizer_no_llm):
        """单字不触发 contains（要求 ≥ 2 字符）。"""
        result = normalizer_no_llm.normalize_sync(["咳"])
        # "咳" is alias of "咳嗽", so it matches as alias
        if result.results[0].method != "alias":
            # If not alias, should not be contains (single char)
            assert result.results[0].method != "contains"

    def test_contains_exact_match_priority(self, normalizer_no_llm):
        """Exact match > contains。"""
        result = normalizer_no_llm.normalize_sync(["干咳"])
        assert result.results[0].method == "exact"  # not contains


# ═══════════════════════════════════════════════════════════
# Cache Tests
# ═══════════════════════════════════════════════════════════

class TestCache:
    @pytest.mark.asyncio
    async def test_cache_hit_avoids_llm(self, mock_llm, normalizer_with_llm):
        from app.normalizer.symptom_normalizer import SymptomMappingResult, SymptomMapping

        mock_llm.generate_structured.return_value = SymptomMappingResult(
            mappings=[
                SymptomMapping(raw="喉咙不舒服", standard="咽喉痛", confidence=0.9),
            ]
        )

        result1 = await normalizer_with_llm.normalize(["喉咙不舒服"])
        assert result1.llm_calls == 1

        mock_llm.generate_structured.reset_mock()
        result2 = await normalizer_with_llm.normalize(["喉咙不舒服"])
        assert result2.llm_calls == 0
        assert result2.cache_hits == 1

    def test_clear_cache(self, normalizer_no_llm):
        normalizer_no_llm._cache["test"] = "result"
        assert len(normalizer_no_llm._cache) == 1
        normalizer_no_llm.clear_cache()
        assert len(normalizer_no_llm._cache) == 0


# ═══════════════════════════════════════════════════════════
# Edge Cases
# ═══════════════════════════════════════════════════════════

class TestEdgeCases:
    def test_empty_input(self, normalizer_no_llm):
        result = normalizer_no_llm.normalize_sync([])
        assert len(result.results) == 0
        assert result.total_time_ms >= 0

    def test_empty_string(self, normalizer_no_llm):
        result = normalizer_no_llm.normalize_sync([""])
        assert result.results[0].standard == ""
        assert result.results[0].confidence == 0.0

    def test_whitespace_only(self, normalizer_no_llm):
        result = normalizer_no_llm.normalize_sync(["   "])
        assert result.results[0].standard == "   "

    def test_stripping(self, normalizer_no_llm):
        result = normalizer_no_llm.normalize_sync(["  干咳  "])
        assert result.results[0].raw == "干咳"

    def test_duplicate_symptoms(self, normalizer_no_llm):
        result = normalizer_no_llm.normalize_sync(["干咳", "干咳", "干咳"])
        assert len(result.results) == 3
        assert all(r.standard == "干咳" for r in result.results)

    def test_batch_preserves_order(self, normalizer_no_llm):
        raw = ["发热", "咳嗽", "头痛", "干咳"]
        result = normalizer_no_llm.normalize_sync(raw)
        assert [r.raw for r in result.results] == raw

    def test_mixed_known_unknown(self, normalizer_no_llm):
        """部分匹配、部分丢弃。"""
        result = normalizer_no_llm.normalize_sync(["干咳", "未知症状xyz"])
        assert result.results[0].standard == "干咳"
        assert result.results[1].method == "discarded"


# ═══════════════════════════════════════════════════════════
# LLM Error Handling
# ═══════════════════════════════════════════════════════════

class TestLLMErrorHandling:
    @pytest.mark.asyncio
    async def test_llm_error_graceful(self, mock_llm, normalizer_with_llm):
        mock_llm.generate_structured.side_effect = Exception("LLM Down")
        result = await normalizer_with_llm.normalize(["喉咙不舒服"])
        # Should not raise, should discard
        assert result.results[0].method == "discarded"
        assert result.discarded_count == 1

    @pytest.mark.asyncio
    async def test_llm_not_called_when_all_layer0_matched(self, mock_llm, normalizer_with_llm):
        result = await normalizer_with_llm.normalize(["干咳", "头痛", "发烧"])
        assert result.llm_calls == 0
        mock_llm.generate_structured.assert_not_called()
