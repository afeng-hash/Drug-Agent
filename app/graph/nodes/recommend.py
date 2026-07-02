"""
Recommend node — 症状 → OTC 药品匹配推荐。

这是推荐环节的核心节点：
  1. 从 DB 查询候选药品
  2. 用 LLM 对药品排序（1-3 个最合适的）
  3. 对每个推荐药品，从 Milvus (RAG) 检索说明书片段
  4. 拼装包含作用功效、用法用量、注意事项的完整推荐回复

数据来源分工：
  - PostgreSQL → 药品元数据（名称、商品名、剂型、价格）
  - Milvus/RAG → 药品说明书（适应症、不良反应、禁忌、注意事项）
  - LLM       → 症状匹配排序 + 推荐理由生成
"""

import asyncio
import json

from pydantic import BaseModel, Field

from app.db.repositories.drug import DrugRepository
from app.llm.client import LLMClient
from app.rag.retriever import DrugManualRetriever
from app.rag.schemas import Chunk


# ── LLM 结构化输出 Schema ────────────────────────────────

class RecommendationItem(BaseModel):
    """单个推荐药品。"""
    drug_id: int
    generic_name: str
    match_reason: str = Field(
        description="通俗的推荐理由，结合用户具体症状说明为什么推荐这个药，2-3句话"
    )
    score: float = Field(ge=0.0, le=1.0)


class RecommendOutput(BaseModel):
    """LLM 排序输出的推荐列表。"""
    recommendations: list[RecommendationItem] = Field(max_length=3)


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
    retriever: DrugManualRetriever,
) -> dict:
    """执行药品推荐 + RAG 说明书检索。

    Args:
        state:     当前对话状态
        llm_client: LLM 客户端
        drug_repo:  药品仓库（已绑定 DB session）
        retriever:  Milvus 药品说明书检索器

    Returns:
        state 更新 dict：
          - recommendations → [{drug_id, generic_name, match_reason, score}]
          - response        → 含作用功效、用法用量、注意事项的推荐回复
          - phase           → "recommending"
          - node_events     → 节点事件日志
    """
    slots = state.get("consult_slots", {})
    summary = state.get("consult_summary", "")
    safety_result = state.get("safety_result") or {}
    excluded_drugs = safety_result.get("excluded_drugs", [])

    # ── 1. 从槽位提取症状名称 ──
    symptoms = slots.get("symptoms", [])
    symptom_names = _extract_symptom_names(symptoms)

    # ── 2. 从 DB 查询候选药品 ──
    drugs = await drug_repo.find_by_symptoms(symptom_names, category="感冒退烧")
    if not drugs:
        drugs = await drug_repo.list_all(category="感冒退烧")

    # ── 3. 排除安全规则过滤的药品 ──
    candidates = [d for d in drugs if d.generic_name not in excluded_drugs]
    if not candidates:
        return {
            "recommendations": [],
            "response": "抱歉，根据您的情况，目前没有合适的OTC药品推荐。建议您咨询药师或就医。",
            "phase": "ended",
            "node_events": [{"node": "recommend", "count": 0}],
        }

    # ── 4. LLM 排序推荐 ──
    output = await _rank_drugs(llm_client, candidates, summary, symptom_names)

    recommendations = [
        {
            "drug_id": r.drug_id,
            "generic_name": r.generic_name,
            "match_reason": r.match_reason,
            "score": r.score,
        }
        for r in output.recommendations
    ]

    # ── 5. RAG 检索：对每个推荐药品查说明书 ──
    rag_map = await _fetch_rag_batch(retriever, recommendations)

    # ── 6. 拼装回复 ──
    response = _build_response(
        candidates=candidates,
        recommendations=recommendations,
        rag_map=rag_map,
        slots=slots,
    )

    return {
        "recommendations": recommendations,
        "response": response,
        "phase": "recommending",
        "node_events": [{"node": "recommend", "count": len(recommendations)}],
    }


# ──────────────────────────────────────────────────────────
# 内部辅助函数
# ──────────────────────────────────────────────────────────


def _extract_symptom_names(symptoms: list) -> list[str]:
    """从槽位的 symptoms 列表提取纯文本名称。"""
    return [
        s.get("name", s) if isinstance(s, dict) else str(s)
        for s in symptoms
    ]


async def _rank_drugs(
    llm_client: LLMClient,
    candidates: list,
    summary: str,
    symptom_names: list[str],
) -> RecommendOutput:
    """用 LLM 对候选药品排序，挑出最好的 1-3 个。"""
    drug_list = [
        {
            "id": d.id,
            "generic_name": d.generic_name,
            "brand_names": d.brand_names,
            "indication": d.indication_summary,
            "active_ingredients": d.active_ingredients,
            "dosage_form": d.dosage_form,
        }
        for d in candidates
    ]

    prompt = (
        f"## 用户症状摘要\n{summary}\n\n"
        f"## 候选药品 (共{len(candidates)}种)\n"
        f"{json.dumps(drug_list, ensure_ascii=False, indent=2)}\n\n"
        f"请从中选择 1-3 个最适合的药品，按推荐度从高到低排序。\n"
        f"每个药品给出通俗的推荐理由（2-3句话），要结合用户的具体症状说明为什么选这个药"
        f"（例如：'您主要是头痛和发热，对乙酰氨基酚退热效果明确且对胃肠刺激小，适合作为首选。'）\n"
        f"score 表示推荐度（0-1），最高推荐度为 1.0。"
    )

    try:
        return await llm_client.generate_structured(
            messages=[
                {"role": "system", "content": "你是OTC药品推荐专家，根据症状匹配最合适的药品。只输出JSON。"},
                {"role": "user", "content": prompt},
            ],
            schema=RecommendOutput,
            temperature=0.3,
            max_tokens=1024,
        )
    except Exception:
        # LLM 不可用时降级：取前 3 个候选，给出通用理由
        return RecommendOutput(
            recommendations=[
                RecommendationItem(
                    drug_id=d.id,
                    generic_name=d.generic_name,
                    match_reason=f"适用于缓解{', '.join(symptom_names) if symptom_names else '相关'}症状",
                    score=0.5,
                )
                for d in candidates[:3]
            ]
        )


async def _fetch_rag_batch(
    retriever: DrugManualRetriever,
    recommendations: list[dict],
) -> dict[int, list[Chunk]]:
    """并行查询 RAG：对每个推荐药品检索说明书片段。

    用 asyncio.gather 并行请求，3 个药品约 200ms（串行约 600ms）。
    Milvus 不可用时静默降级，返回空 dict。
    """
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

    tasks = [
        _fetch_one(r["drug_id"], r["generic_name"])
        for r in recommendations
    ]
    results = await asyncio.gather(*tasks)
    return {drug_id: chunks for drug_id, chunks in results}


def _build_response(
    candidates: list,
    recommendations: list[dict],
    rag_map: dict[int, list[Chunk]],
    slots: dict,
) -> str:
    """拼装最终推荐回复：DB 数据 + LLM 排序 + RAG 说明书。"""
    lines = ["根据您的情况，为您推荐以下药品：\n"]

    for i, rec in enumerate(recommendations, 1):
        drug = _find_candidate(candidates, rec["drug_id"])
        brands = f"（{'、'.join(drug.brand_names)}）" if drug and drug.brand_names else ""
        chunks = rag_map.get(rec["drug_id"], [])

        lines.append("---")
        lines.append(f"### {i}. **{rec['generic_name']}**{brands}\n")
        lines.append(f"**推荐理由**：{rec['match_reason']}\n")

        # ── 作用功效（RAG 优先 → DB 降级）──
        efficacy = _extract_efficacy(chunks, drug)
        if efficacy:
            lines.append(f"**作用功效**：{efficacy}\n")

        # ── 用法用量（DB 优先，年龄适配）──
        usage = _extract_usage(drug, slots, chunks)
        if usage:
            lines.append(f"**用法用量**：{usage}\n")

        # ── 关键警示（RAG：禁忌 + 注意事项 1-2 条）──
        warnings = _extract_warnings(chunks)
        if warnings:
            lines.append(f"**⚠️ 注意**：{warnings}\n")

        lines.append("")

    return "\n".join(lines) + DISCLAIMER


def _find_candidate(candidates: list, drug_id: int):
    """从候选列表中按 ID 查找 Drug 对象。"""
    return next((d for d in candidates if d.id == drug_id), None)


def _extract_efficacy(chunks: list[Chunk], drug) -> str:
    """从 RAG chunks 提取作用功效描述。

    策略：
      1. 优先取 section=="通用" 的 chunk（适应症通常在通用段落）
      2. 取第一个有效 chunk 的前 200 字
      3. 降级用 DB indication_summary
    """
    if chunks:
        # 按相似度排序后取最好的 chunk
        sorted_chunks = sorted(chunks, key=lambda c: c.score, reverse=True)
        for chunk in sorted_chunks:
            text = chunk.content.strip()
            # 跳过明显是禁忌/不良反应的内容
            if any(kw in text[:20] for kw in ("禁忌", "不良反应", "药物相互作用", "相互作用")):
                continue
            if len(text) > 15:  # 足够长的内容才用
                return _truncate(text, 200)
        # 所有 chunk 都被跳过了，取第一个
        if sorted_chunks:
            return _truncate(sorted_chunks[0].content, 200)

    # 降级：DB 适应症
    if drug:
        return drug.indication_summary
    return ""


def _extract_usage(drug, slots: dict, chunks: list[Chunk]) -> str:
    """提取年龄适配的用法用量。

    策略：
      1. DB usage_* 字段已按年龄分组 → 优先用 DB（结构化、可靠）
      2. RAG chunks 作为补充（如 DB 字段为空）
    """
    if not drug:
        return ""

    age = slots.get("age")
    special_pop = slots.get("special_population")

    # 判断用户年龄段
    if special_pop in ("儿童", "child") or (age and age < 12):
        usage = drug.usage_child
    elif special_pop in ("老人", "elderly") or (age and age >= 60):
        usage = drug.usage_elderly or drug.usage_adult
    else:
        usage = drug.usage_adult

    if usage:
        return usage

    # DB 没有 → 从 RAG 找
    if chunks:
        for chunk in sorted(chunks, key=lambda c: c.score, reverse=True):
            text = chunk.content
            if any(kw in text for kw in ("一次", "每日", "成人", "用法", "用量")):
                return _truncate(text, 150)
    return ""


def _extract_warnings(chunks: list[Chunk]) -> str:
    """从 RAG chunks 提取关键警示（禁忌/注意事项/不良反应）。

    策略：
      - 取 1-2 条最关键的警示（来自 section=="禁忌" 或 "注意事项" 的 chunk）
      - 取每条的第一句话（最核心的信息）
    """
    if not chunks:
        return ""

    warning_chunks = [
        c for c in chunks
        if c.section in ("禁忌", "注意事项", "不良反应")
    ]
    if not warning_chunks:
        # 没有明确 section，看内容关键词
        warning_chunks = [
            c for c in chunks
            if any(kw in c.content for kw in ("禁用", "慎用", "避免", "注意", "不宜"))
        ]

    warnings = []
    for chunk in warning_chunks[:3]:
        # 取第一句话
        text = chunk.content.strip()
        sentences = text.replace("\n", " ").split("。")
        first = sentences[0].strip() + "。"
        if first and first not in warnings and len(first) > 5:
            warnings.append(first)
        if len(warnings) >= 2:
            break

    return " ".join(warnings) if warnings else ""


def _truncate(text: str, max_len: int) -> str:
    """截断文本到指定长度，保证不截断中文字符。"""
    if len(text) <= max_len:
        return text
    return text[:max_len].rsplit("。", 1)[0] + "。"
