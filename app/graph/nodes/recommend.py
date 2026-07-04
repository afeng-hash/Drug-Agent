"""
Recommend node — 症状 → OTC 药品匹配推荐。

流程:
  1. DB 查候选药品
  2. ScoringPipeline 确定性评分排序（替代 LLM 排序）
  3. RAG 检索说明书片段
  4. LLM 生成推荐理由文案（仅文案，不参与排序）
  5. 拼装回复

数据来源分工：
  - PostgreSQL  → 药品元数据 + 权重配置
  - ScoringPipeline → 确定性评分排序（Evidence → Features → Weighted Score）
  - Milvus/RAG → 药品说明书（作用功效、注意事项）
  - LLM       → 推荐理由文案（结合症状的自然语言解释）
"""

import asyncio
import json

from pydantic import BaseModel, Field

from app.db.repositories.drug import DrugRepository
from app.db.repositories.weight_config import WeightConfigRepository
from app.llm.client import LLMClient
from app.rag.retriever import DrugManualRetriever
from app.rag.schemas import Chunk
from app.scorer.pipeline import ScoringPipeline


# ── LLM 文案生成 Schema ──────────────────────────────────

class DrugReasonItem(BaseModel):
    """LLM 为单个药品生成的推荐文案。"""
    drug_id: int
    generic_name: str
    match_reason: str = Field(
        description="通俗推荐理由，2-3句，结合用户具体症状说明为什么推荐"
    )


class ReasonOutput(BaseModel):
    """LLM 生成的批量推荐理由。"""
    reasons: list[DrugReasonItem] = Field(max_length=3)


# ── 回复模板 ──────────────────────────────────────────────

DISCLAIMER = (
    "\n\n---\n"
    "📋 **免责声明**：本系统仅为辅助参考，请仔细阅读说明书并按说明使用，"
    "或在药师指导下购买和使用。如症状持续或加重，请及时就医。"
)


async def recommend_node(
    state: dict,
    llm_client: LLMClient,
    drug_repo: DrugRepository,
    weight_repo: WeightConfigRepository,
    retriever: DrugManualRetriever,
    scoring_pipeline: ScoringPipeline,
    drug_graph_repo=None,
) -> dict:
    """执行药品推荐：评分排序 + RAG 说明 + LLM 文案。

    Args:
        state:             当前对话状态
        llm_client:        LLM 客户端（仅用于生成推荐理由文案）
        drug_repo:         药品仓库（已绑定 DB session，PG 降级备用）
        weight_repo:       权重配置仓库（已绑定 DB session）
        retriever:         Milvus 说明书检索器
        scoring_pipeline:  评分排序管线
        drug_graph_repo:   Neo4j 图谱仓库（主查询路径，None 时降级到 PG）

    Returns:
        state 更新 dict。
    """
    slots = state.get("consult_slots", {})
    summary = state.get("consult_summary", "")
    session_id = state.get("session_id", "")
    safety_result = state.get("safety_result") or {}
    excluded_drugs = safety_result.get("excluded_drugs", [])

    # ── 1. 提取症状名称并分配权重 ──
    # 主诉症状 weight=1.0，附加症状 weight=0.5
    symptoms = slots.get("symptoms", [])
    primary_names = _extract_symptom_names(symptoms)
    secondary_names = _extract_symptom_names(slots.get("other_symptoms", []))

    symptom_weights = (
        [{"name": n, "weight": 1.0} for n in primary_names]
        + [{"name": n, "weight": 0.5} for n in secondary_names]
    )
    symptom_names = primary_names + secondary_names

    # ── 2. 查询候选药品（Neo4j 优先 → PG ILIKE 降级）──
    # todo
    candidates = await _fetch_candidates(
        drug_graph_repo=drug_graph_repo,
        drug_repo=drug_repo,
        symptom_weights=symptom_weights,
        symptom_names=symptom_names,
        category="感冒退烧",
    )

    # ── 3. 排除安全规则过滤的药品 ──
    candidates = [d for d in candidates if d.generic_name not in excluded_drugs]
    if not candidates:
        return {
            "recommendations": [],
            "response": "抱歉，根据您的情况，目前没有合适的OTC药品推荐。建议您咨询药师或就医。",
            "phase": "ended",
            "node_events": [{"node": "recommend", "count": 0}],
        }

    # ── 4. ScoringPipeline 确定性评分排序 ──
    scoring_result = await scoring_pipeline.run(
        candidates=candidates,
        slots=slots,
        session_id=session_id,
        weight_repo=weight_repo,
    )

    # Filter out excluded drugs, take top 3
    active_drugs = [sd for sd in scoring_result.drugs if not sd.excluded]
    top_drugs = active_drugs[:3]

    if not top_drugs:
        return {
            "recommendations": [],
            "response": "抱歉，根据您的安全筛查结果，目前没有合适的OTC药品推荐。建议您咨询药师或就医。",
            "phase": "ended",
            "node_events": [{"node": "recommend", "count": 0}],
        }

    # ── 5. LLM 生成推荐理由文案（仅文案，不参与排序）──
    top_candidates = [d for d in candidates if d.id in {sd.drug_id for sd in top_drugs}]
    reasons_map = await _generate_reasons(llm_client, top_candidates, summary, symptom_names)

    # ── 6. RAG 检索说明书 ──
    rag_map = await _fetch_rag_batch(retriever, top_drugs)

    # ── 7. 拼装回复 ──
    recommendations = [
        {
            "drug_id": sd.drug_id,
            "generic_name": sd.generic_name,
            "match_reason": reasons_map.get(sd.drug_id, "根据您的症状匹配推荐"),
            "score": sd.total_score,
        }
        for sd in top_drugs
    ]

    response = _build_response(
        candidates=candidates,
        recommendations=recommendations,
        scored_drugs=top_drugs,
        rag_map=rag_map,
        slots=slots,
    )

    return {
        "recommendations": recommendations,
        "response": response,
        "phase": "recommending",
        "node_events": [{
            "node": "recommend",
            "count": len(recommendations),
            "config_version": scoring_result.config_version,
            "scoring_ms": scoring_result.total_time_ms,
        }],
    }


# ──────────────────────────────────────────────────────────
# 辅助函数
# ──────────────────────────────────────────────────────────


def _extract_symptom_names(symptoms: list) -> list[str]:
    """从槽位 symptoms 列表提取纯文本名称。"""
    return [
        s.get("name", s) if isinstance(s, dict) else str(s)
        for s in symptoms
    ]


async def _fetch_candidates(
    drug_graph_repo,
    drug_repo: DrugRepository,
    symptom_weights: list[dict],
    symptom_names: list[str],
    category: str,
) -> list:
    """查询候选药品：Neo4j 图查询优先，PG ILIKE 降级。

    Neo4j 返回排好序的药品名 → 从 PG 补全 Drug ORM 对象。
    降级时直接用 PG ILIKE 查询。

    Args:
        drug_graph_repo: DrugGraphRepository or None
        drug_repo:       PG DrugRepository (always available)
        symptom_weights: [{name, weight}, ...] for Neo4j scoring
        symptom_names:   plain name list for PG ILIKE fallback
        category:        drug category filter

    Returns:
        List of Drug ORM objects, ordered by relevance (Neo4j score or PG ILIKE order)
    """
    # ── 优先：Neo4j 图查询 ──
    if drug_graph_repo is not None:
        try:
            kg_candidates = await drug_graph_repo.find_candidates_by_symptoms(
                symptoms=symptom_weights,
                categories=[category],
            )
            if kg_candidates:
                # Map Neo4j drug names → PG Drug ORM objects
                kg_names = [c.generic_name for c in kg_candidates]
                drugs_map = {d.generic_name: d for d in await drug_repo.find_by_ids_names(kg_names)}
                # Preserve KG ordering, only keep drugs found in PG
                result = [drugs_map[name] for name in kg_names if name in drugs_map]
                if result:
                    return result
        except Exception:
            pass  # KG query failed → fall through to PG fallback

    # ── 降级：PG ILIKE ──
    drugs = await drug_repo.find_by_symptoms(symptom_names, category=category)
    if not drugs:
        drugs = await drug_repo.list_all(category=category)
    return drugs


async def _generate_reasons(
    llm_client: LLMClient,
    top_candidates: list,
    summary: str,
    symptom_names: list[str],
) -> dict[int, str]:
    """用 LLM 为排名后的药品生成推荐理由文案。

    注意：LLM 不参与排序，只生成文案。排序由 ScoringPipeline 完成。
    """
    drug_info = [
        {
            "id": d.id,
            "generic_name": d.generic_name,
            "indication": d.indication_summary,
        }
        for d in top_candidates
    ]

    prompt = (
        f"## 用户症状\n{summary}\n\n"
        f"## 推荐药品（已按评分排序，不可改变顺序）\n"
        f"{json.dumps(drug_info, ensure_ascii=False, indent=2)}\n\n"
        f"请为每个药品生成一句通俗易懂的推荐理由（2-3句话）。\n"
        f"要结合用户的具体症状，说明为什么这个药适合。\n"
        f"不要改变药品顺序。"
    )

    try:
        output = await llm_client.generate_structured(
            messages=[
                {"role": "system", "content": "你是药店店员，用通俗语言向顾客解释药品推荐理由。"},
                {"role": "user", "content": prompt},
            ],
            schema=ReasonOutput,
            temperature=0.3,
            max_tokens=512,
        )
        return {r.drug_id: r.match_reason for r in output.reasons}
    except Exception:
        # LLM 不可用 → 用通用理由
        return {
            d.id: f"适用于缓解{', '.join(symptom_names) if symptom_names else '相关'}症状"
            for d in top_candidates
        }


async def _fetch_rag_batch(
    retriever: DrugManualRetriever,
    top_drugs: list,
) -> dict[int, list[Chunk]]:
    """并行 RAG 检索：对每个推荐药品查说明书片段。"""
    async def _fetch_one(drug_id: int, drug_name: str) -> tuple[int, list[Chunk]]:
        try:
            chunks = await retriever.retrieve(
                drug_name=drug_name,
                query="适应症 用法用量 注意事项 禁忌 不良反应",
                top_k=4,
            )
            return drug_id, chunks
        except Exception:
            return drug_id, []

    tasks = [_fetch_one(sd.drug_id, sd.generic_name) for sd in top_drugs]
    results = await asyncio.gather(*tasks)
    return {drug_id: chunks for drug_id, chunks in results}


def _build_response(
    candidates: list,
    recommendations: list[dict],
    scored_drugs: list,
    rag_map: dict[int, list[Chunk]],
    slots: dict,
) -> str:
    """拼装最终推荐回复。"""
    lines = ["根据您的情况，为您推荐以下药品：\n"]

    for i, (rec, sd) in enumerate(zip(recommendations, scored_drugs), 1):
        drug = _find_candidate(candidates, rec["drug_id"])
        brands = f"（{'、'.join(drug.brand_names)}）" if drug and drug.brand_names else ""
        chunks = rag_map.get(rec["drug_id"], [])

        lines.append("---")
        lines.append(f"### {i}. **{rec['generic_name']}**{brands}")
        lines.append(f"**评分**: {rec['score']:.2f}  |  **推荐理由**: {rec['match_reason']}\n")

        # 作用功效（RAG 优先 → DB 降级）
        efficacy = _extract_efficacy(chunks, drug)
        if efficacy:
            lines.append(f"**作用功效**：{efficacy}\n")

        # 用法用量（DB 优先，年龄适配）
        usage = _extract_usage(drug, slots, chunks)
        if usage:
            lines.append(f"**用法用量**：{usage}\n")

        # 关键警示（RAG）
        warnings = _extract_warnings(chunks)
        if warnings:
            lines.append(f"**⚠️ 注意**：{warnings}\n")

        lines.append("")

    return "\n".join(lines) + DISCLAIMER


def _find_candidate(candidates: list, drug_id: int):
    return next((d for d in candidates if d.id == drug_id), None)


def _extract_efficacy(chunks: list[Chunk], drug) -> str:
    if chunks:
        sorted_chunks = sorted(chunks, key=lambda c: c.score, reverse=True)
        for chunk in sorted_chunks:
            text = chunk.content.strip()
            if any(kw in text[:20] for kw in ("禁忌", "不良反应", "药物相互作用")):
                continue
            if len(text) > 15:
                return _truncate(text, 200)
        if sorted_chunks:
            return _truncate(sorted_chunks[0].content, 200)
    if drug:
        return drug.indication_summary
    return ""


def _extract_usage(drug, slots: dict, chunks: list[Chunk]) -> str:
    if not drug:
        return ""
    age = slots.get("age")
    special_pop = slots.get("special_population")
    if special_pop in ("儿童", "child") or (age is not None and age < 12):
        usage = drug.usage_child
    elif special_pop in ("老人", "elderly") or (age is not None and age >= 60):
        usage = drug.usage_elderly or drug.usage_adult
    else:
        usage = drug.usage_adult
    if usage:
        return usage
    if chunks:
        for chunk in sorted(chunks, key=lambda c: c.score, reverse=True):
            if any(kw in chunk.content for kw in ("一次", "每日", "成人", "用法")):
                return _truncate(chunk.content, 150)
    return ""


def _extract_warnings(chunks: list[Chunk]) -> str:
    if not chunks:
        return ""
    warning_chunks = [c for c in chunks if c.section in ("禁忌", "注意事项", "不良反应")]
    if not warning_chunks:
        warning_chunks = [c for c in chunks if any(
            kw in c.content for kw in ("禁用", "慎用", "避免", "注意", "不宜")
        )]
    warnings = []
    for chunk in warning_chunks[:3]:
        text = chunk.content.strip()
        sentences = text.replace("\n", " ").split("。")
        first = sentences[0].strip() + "。"
        if first and first not in warnings and len(first) > 5:
            warnings.append(first)
        if len(warnings) >= 2:
            break
    return " ".join(warnings) if warnings else ""


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len].rsplit("。", 1)[0] + "。"
