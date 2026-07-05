"""症状标准化引擎 — 两级匹配策略将自由文本症状名映射到 KG 标准词表。

Layer 0 (确定性, <1ms):  exact → alias → contains
Layer 1 (LLM + 硬词表约束):  仅对 Layer 0 未匹配且风险可接受的症状调用
"""

import json
import logging
import time

from pydantic import BaseModel, Field

from app.normalizer.schemas import NormalizedSymptom, NormalizationResult
from app.normalizer.vocabulary import SymptomEntry, VocabularySource

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# 风险分层阈值
# ═══════════════════════════════════════════════════════════════

# Level 1 (粗粒度): LLM 映射需 ≥ 此值才接受
L1_CONFIDENCE_THRESHOLD = 0.7

# Level 2 (中等粒度): LLM 映射需 ≥ 此值才接受
L2_CONFIDENCE_THRESHOLD = 0.85

# Level 3 (细粒度): 不走 LLM，Layer 0 未匹配即丢弃

# LLM 映射的默认置信度（LLM 本身不返回置信度时使用）
LLM_DEFAULT_CONFIDENCE = 0.75


# ═══════════════════════════════════════════════════════════════
# LLM Fallback 输出 Schema
# ═══════════════════════════════════════════════════════════════


class SymptomMapping(BaseModel):
    """LLM 返回的单个症状映射。"""
    raw: str = Field(description="原始症状文本")
    standard: str | None = Field(
        description="匹配到的标准 KG 症状名。无法匹配时为 null"
    )
    confidence: float = Field(
        default=0.0,
        description="映射置信度 0.0~1.0，基于语义匹配程度",
    )


class SymptomMappingResult(BaseModel):
    """LLM 返回的批量映射结果。"""
    mappings: list[SymptomMapping] = Field(description="每个输入症状的映射，顺序与输入一致")


# ═══════════════════════════════════════════════════════════════
# 主类
# ═══════════════════════════════════════════════════════════════


class SymptomNormalizer:
    """症状标准化引擎。

    使用方式：
        normalizer = SymptomNormalizer(vocab=vocab_source, llm_client=llm_client)
        result = await normalizer.normalize(["喉咙不舒服", "干咳"])
    """

    def __init__(self, vocab: VocabularySource, llm_client=None):
        """
        Args:
            vocab:      VocabularySource 实例（已 load）
            llm_client: LLMClient 实例（可选，用于 Layer 1）
        """
        self._vocab = vocab
        self._llm_client = llm_client
        # 缓存：raw_text → standard_name（None 表示 LLM 判定无法匹配）
        self._cache: dict[str, str | None] = {}

    # ── 公共 API ──────────────────────────────────────────

    async def normalize(self, raw_names: list[str]) -> NormalizationResult:
        """主入口（异步）：批量标准化症状名称。"""
        return await self._normalize_impl(raw_names, use_llm=True)

    def normalize_sync(self, raw_names: list[str]) -> NormalizationResult:
        """同步版 normalize（仅 Layer 0，不调用 LLM）。"""
        import asyncio
        return asyncio.run(self._normalize_impl(raw_names, use_llm=False))

    async def _normalize_impl(
        self, raw_names: list[str], use_llm: bool
    ) -> NormalizationResult:
        """标准化核心实现（Layer 0 + 可选 Layer 1）。"""
        t0 = time.perf_counter()
        llm_calls = 0
        cache_hits = 0
        discarded_count = 0

        results: list[NormalizedSymptom | None] = []
        unmatched: list[str] = []
        unmatched_indices: list[int] = []

        # ── Layer 0 ──
        for i, raw in enumerate(raw_names):
            if not raw or not raw.strip():
                results.append(NormalizedSymptom(
                    raw=raw, standard=raw, confidence=0.0,
                    method="exact", level=0,
                ))
                continue
            raw_clean = raw.strip()
            match = self._match_layer0(raw_clean)
            if match is not None:
                results.append(match)
            else:
                results.append(None)
                unmatched.append(raw_clean)
                unmatched_indices.append(i)

        # ── Layer 1 ──
        if unmatched and use_llm and self._llm_client is not None:
            llm_mappings, llm_actually_called, new_cache_hits = \
                await self._match_layer1(unmatched)
            if llm_actually_called:
                llm_calls = 1
            cache_hits = new_cache_hits

            for idx, raw in zip(unmatched_indices, unmatched):
                mapped = llm_mappings.get(raw)
                if mapped is not None:
                    target_entry = self._vocab.get_by_name(mapped)
                    if target_entry is None:
                        discarded_count += 1
                        results[idx] = self._make_discarded(raw)
                    elif target_entry.level == 3:
                        discarded_count += 1
                        results[idx] = self._make_discarded(raw)
                    else:
                        confidence = LLM_DEFAULT_CONFIDENCE
                        if self._risk_accept(target_entry, confidence):
                            results[idx] = NormalizedSymptom(
                                raw=raw, standard=mapped,
                                confidence=confidence, method="llm",
                                level=target_entry.level,
                            )
                        else:
                            discarded_count += 1
                            results[idx] = self._make_discarded(raw)
                else:
                    discarded_count += 1
                    results[idx] = self._make_discarded(raw)

        elif unmatched:
            for idx, raw in zip(unmatched_indices, unmatched):
                discarded_count += 1
                results[idx] = self._make_discarded(raw)

        elapsed = (time.perf_counter() - t0) * 1000

        #确保所有条目均为 NormalizedSymptom 类型（替换掉 None 占位符）
        final_results: list[NormalizedSymptom] = [
            r if r is not None else SymptomNormalizer._make_discarded("")
            for r in results
        ]

        return NormalizationResult(
            results=final_results,
            total_time_ms=round(elapsed, 3),
            llm_calls=llm_calls,
            cache_hits=cache_hits,
            discarded_count=discarded_count,
        )

    # ── Layer 0: 确定性匹配 ──────────────────────────────

    def _match_layer0(self, raw: str) -> NormalizedSymptom | None:
        """exact → alias → contains。命中返回 NormalizedSymptom，未命中返回 None。"""
        # 1) Exact match
        entry = self._vocab.get_by_name(raw)
        if entry is not None:
            return NormalizedSymptom(
                raw=raw, standard=entry.name,
                confidence=1.0, method="exact", level=entry.level,
            )

        # 2) Alias match
        resolved = self._vocab.resolve_alias(raw)
        if resolved is not None:
            entry = self._vocab.get_by_name(resolved)
            return NormalizedSymptom(
                raw=raw, standard=resolved,
                confidence=1.0, method="alias",
                level=entry.level if entry else 1,
            )

        # 3) Contains match — 双向包含，取最长命中
        best = self._contains_match(raw)
        if best is not None:
            standard_name, match_text = best
            entry = self._vocab.get_by_name(standard_name)
            return NormalizedSymptom(
                raw=raw, standard=standard_name,
                confidence=0.80, method="contains",
                level=entry.level if entry else 1,
            )

        return None

    def _contains_match(self, raw: str) -> tuple[str, str] | None:
        """双向包含匹配。返回 (standard_name, matched_text) 或 None。

        从所有标准名和别名中找最长命中的：
          - raw 包含 name/alias（如 "一直干咳" contains "干咳"）
          - name/alias 包含 raw（如 "咽部干燥" contains "咽干"）
        """
        best_entry_name: str | None = None
        best_match_len: int = 0

        for entry in self._vocab.all_entries():
            # 检查标准名
            if self._is_contains_match(raw, entry.name):
                if len(entry.name) > best_match_len:
                    best_entry_name = entry.name
                    best_match_len = len(entry.name)

            # 检查别名
            for alias in entry.aliases:
                if self._is_contains_match(raw, alias):
                    if len(alias) > best_match_len:
                        best_entry_name = entry.name
                        best_match_len = len(alias)

        if best_entry_name is not None:
            return (best_entry_name, raw)
        return None

    @staticmethod
    def _is_contains_match(raw: str, target: str) -> bool:
        """检查 raw 和 target 是否存在包含关系。要求 target ≥ 2 字符。"""
        if len(target) < 2:
            return False
        return target in raw or raw in target

    # ── Layer 1: LLM 兜底 ────────────────────────────────

    async def _match_layer1(
        self, raw_names: list[str]
    ) -> tuple[dict[str, str | None], bool, int]:
        """用 LLM 对未匹配症状做语义映射。

        Args:
            raw_names: 需要 LLM 处理的原始症状名列表

        Returns:
            ({raw: standard | None}, llm_was_called, cache_hits)
        """
        if not raw_names:
            return {}, False, 0

        cache_hits = 0
        result: dict[str, str | None] = {}

        # 先查缓存
        uncached: list[str] = []
        for raw in raw_names:
            if raw in self._cache:
                result[raw] = self._cache[raw]
                cache_hits += 1
            else:
                uncached.append(raw)

        if not uncached:
            return result, False, cache_hits

        # 调用 LLM
        try:
            prompt = self._build_llm_prompt(uncached)
            output = await self._llm_client.generate_structured(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是一个医学术语标准化助手。将口语化的症状描述映射到标准症状名称。"
                            "如果某个症状确实无法匹配任何已知标准症状，将 standard 设为 null。"
                            "不要强行匹配，不确定的宁可返回 null。"
                            "注意：症状的 IS_A 层级关系仅用于帮助你理解症状间的医学关系，"
                            "不要凭空编造标准名，只使用列表中提供的名称。"
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                schema=SymptomMappingResult,
                temperature=0.0,
                max_tokens=512,
            )

            for mapping in output.mappings:
                if mapping.standard is not None:
                    # 硬词表约束：验证返回名是否在词表中
                    entry = self._vocab.get_by_name(mapping.standard)
                    if entry is None:
                        # 尝试 alias 解析
                        resolved = self._vocab.resolve_alias(mapping.standard)
                        if resolved is not None and self._vocab.get_by_name(resolved):
                            mapping.standard = resolved
                        else:
                            logger.warning(
                                "LLM returned non-vocab symptom '%s' for '%s' — discarded",
                                mapping.standard, mapping.raw,
                            )
                            mapping.standard = None
                self._cache[mapping.raw] = mapping.standard
                result[mapping.raw] = mapping.standard

            return result, True, cache_hits

        except Exception as exc:
            logger.error("LLM symptom normalization failed: %s", exc)
            # 未缓存的标记为 None
            for raw in uncached:
                self._cache[raw] = None
                result[raw] = None
            return result, True, cache_hits

    def _build_llm_prompt(self, raw_names: list[str]) -> str:
        """构建 LLM prompt，包含词表和 IS_A 层级信息。"""
        vocab_lines = []
        for entry in sorted(self._vocab.all_entries(), key=lambda e: e.name):
            parts = [f"  - {entry.name} (Level {entry.level})"]
            if entry.aliases:
                parts.append(f"    别名：{'、'.join(entry.aliases)}")
            if entry.parents:
                parts.append(f"    父节点(IS_A)：{' → '.join(entry.parents)}")
            vocab_lines.append("\n".join(parts))

        vocab_desc = "\n".join(vocab_lines)

        return (
            f"## 任务\n"
            f"将以下用户描述的症状名称映射到标准症状词表中最匹配的项。\n\n"
            f"## 标准症状词表（含 IS_A 层级关系）\n"
            f"{vocab_desc}\n\n"
            f"## 用户描述的症状\n"
            f"{json.dumps(raw_names, ensure_ascii=False, indent=2)}\n\n"
            f"## 映射规则\n"
            f"1. 优先语义匹配：理解用户症状的含义，结合 IS_A 层级找到最接近的标准名\n"
            f"2. 为每个映射给出 0.0~1.0 的置信度\n"
            f"3. 如果某个症状确实无法匹配任何已知症状 → standard 设为 null，confidence 设为 0.0\n"
            f"4. 不要强行匹配，不确定的宁可返回 null\n"
            f"5. 返回的 mappings 数组顺序必须与输入一致"
        )

    # ── 风险分层判断 ──────────────────────────────────────

    def _risk_accept(self, entry: SymptomEntry, confidence: float) -> bool:
        """根据症状层级和 LLM 置信度判断是否接受此映射。

        Level 1: confidence ≥ 0.7 → 接受
        Level 2: confidence ≥ 0.85 → 接受
        Level 3: 不接受（已在 normalize 中提前丢弃）
        """
        if entry.level == 1:
            return confidence >= L1_CONFIDENCE_THRESHOLD
        elif entry.level == 2:
            return confidence >= L2_CONFIDENCE_THRESHOLD
        else:
            # Level 3 或未知层级 → 不接受
            return False

    # ── 辅助方法 ──────────────────────────────────────────

    @staticmethod
    def _make_discarded(raw: str) -> NormalizedSymptom:
        """构造一个被丢弃的症状结果。"""
        return NormalizedSymptom(
            raw=raw, standard="", confidence=0.0,
            method="discarded", level=0,
        )

    def clear_cache(self) -> None:
        """清空 LLM 结果缓存。"""
        self._cache.clear()
