"""
Recommend node — 症状 → OTC 药品匹配推荐。

流程:
  1. 提取症状名称（所有症状等权）
  2. 症状标准化：自由文本 → KG 标准症状名
  3. Neo4j KG 查候选药品 + 禁忌过滤
  4. ScoringPipeline 确定性评分排序
  5. RAG 检索说明书片段（并行）
  6. LLM 一次调用生成推荐理由 + 完整回复（替代旧的两次调用 + 模板拼接）
  7. 拼装结果写入 state

数据来源分工：
  - Neo4j KG     → 症状→药品映射 + 禁忌关系
  - PostgreSQL   → 药品元数据 + 权重配置
  - ScoringPipeline → 确定性评分排序（Evidence → Features → Weighted Score）
  - Milvus/RAG   → 药品说明书（作用功效、注意事项）
  - LLM          → 推荐理由 + 自然语言回复（结合症状的完整推荐文案）
"""

import asyncio
import json
import logging

from pydantic import BaseModel, Field

from app.db.repositories.drug import DrugRepository
from app.db.repositories.weight_config import WeightConfigRepository
from app.llm.client import LLMClient
from app.normalizer import SymptomNormalizer
from app.rag.retriever import DrugManualRetriever
from app.rag.schemas import Chunk
from app.scorer.pipeline import ScoringPipeline
from app.scorer.engine import normalize_for_display

logger = logging.getLogger(__name__)

# ── LLM 推荐回复 Schema ──────────────────────────────────

class DrugReasonItem(BaseModel):
    """LLM 为单个药品生成的推荐理由。"""
    drug_id: int
    generic_name: str
    match_reason: str = Field(
        description="通俗推荐理由，2-3句，结合用户具体症状说明为什么推荐"
    )


class RecommendOutput(BaseModel):
    """LLM 一次性生成的推荐输出：每个药的推荐理由 + 完整回复文本。"""
    drugs: list[DrugReasonItem] = Field(max_length=3)
    response: str = Field(description="完整的用户回复文本，Markdown格式，不含免责声明")


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
    vocab_source=None,
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
        vocab_source:      症状词表（VocabularySource，已加载）

    Returns:
        state 更新 dict。
    """
    slots = state.get("consult_slots", {})
    summary = state.get("consult_summary", "")
    session_id = state.get("session_id", "")
    # ── 1. 提取症状名称（所有症状等权，不区分主诉/伴随）──
    symptoms = slots.get("symptoms", [])
    symptom_names_raw = _extract_symptom_names(symptoms)
    symptom_weights = [{"name": n, "weight": 1.0} for n in symptom_names_raw]
    symptom_names = list(symptom_names_raw)

    # ── 1.5. 症状标准化：自由文本 → KG 标准症状名 ──
    if symptom_weights and vocab_source is not None:
        normalizer = SymptomNormalizer(vocab=vocab_source, llm_client=llm_client)
        raw_names = [sw["name"] for sw in symptom_weights]
        norm_result = await normalizer.normalize(raw_names)
        for sw, ns in zip(symptom_weights, norm_result.results):
            sw["_raw_name"] = sw["name"]   # 保留原始名（供调试）
            sw["name"] = ns.standard if ns.standard else ns.raw
        logger.info(
            "Symptom normalization: %d symptoms, methods=%s, "
            "llm_calls=%d, cache_hits=%d, discarded=%d, %.2fms",
            len(raw_names),
            [ns.method for ns in norm_result.results],
            norm_result.llm_calls,
            norm_result.cache_hits,
            norm_result.discarded_count,
            norm_result.total_time_ms,
        )

    # ── 1.6 去重：同一标准症状名只保留一次 ──
    seen_names: set[str] = set()
    deduped_weights: list[dict] = []
    for sw in symptom_weights:
        name = sw["name"]
        if name and name not in seen_names:
            seen_names.add(name)
            deduped_weights.append(sw)
    if len(deduped_weights) < len(symptom_weights):
        logger.info(
            "Symptom dedup: %d → %d (removed %d duplicates)",
            len(symptom_weights), len(deduped_weights),
            len(symptom_weights) - len(deduped_weights),
        )
        symptom_weights = deduped_weights
        symptom_names = [sw["name"] for sw in symptom_weights]

    # ── 2. 查询候选药品（KG 唯一数据源）──
    candidates = await _fetch_candidates(
        drug_graph_repo=drug_graph_repo,
        drug_repo=drug_repo,
        symptom_weights=symptom_weights,
        symptom_names=symptom_names,
        category="感冒退烧",
    )

    # ── 3. Neo4j 图谱禁忌过滤 ──
    kg_excluded = await _filter_by_kg_contraindications(
        drug_graph_repo=drug_graph_repo,
        candidates=candidates,
        slots=slots,
    )

    candidates = [d for d in candidates if d.generic_name not in kg_excluded]
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

    # ── 4.5 对外展示归一化（批次内 min-max → 0-100） ──
    scoring_result.drugs = normalize_for_display(scoring_result.drugs)

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

    # ── 5. RAG 检索说明书 ──
    rag_map = await _fetch_rag_batch(retriever, top_drugs)

    # ── 6. LLM 生成推荐回复（理由 + 完整回复，一次调用）──
    top_candidates = [d for d in candidates if d.id in {sd.drug_id for sd in top_drugs}]
    drug_data = _prepare_drug_data(top_drugs, top_candidates, rag_map, slots)
    output = await _generate_recommend_response(llm_client, drug_data, summary)

    # ── 7. 拼装结果 ──
    reasons_map = {r.drug_id: r.match_reason for r in output.drugs}
    recommendations = [
        {
            "drug_id": sd.drug_id,
            "generic_name": sd.generic_name,
            "match_reason": reasons_map.get(sd.drug_id, "根据您的症状匹配推荐"),
            "score": sd.display_score,
        }
        for sd in top_drugs
    ]

    return {
        "recommendations": recommendations,
        "response": output.response,
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


async def _filter_by_kg_contraindications(
    drug_graph_repo,
    candidates: list,
    slots: dict,
) -> list[str]:
    """通过 Neo4j 知识图谱查询每个候选药的禁忌关系，排除不安全的药品。

    检查三个维度：
      1. 用户慢性病史 (chronic_conditions) vs 药品禁忌病症 (CONTRAINDICATED_FOR→Condition)
      2. 特殊人群 (special_population) vs 药品禁忌人群 (CONTRAINDICATED_FOR→Population)
      3. 过敏史 (allergies) vs 药品成分 (HAS_INGREDIENT→Ingredient)

    Args:
        drug_graph_repo: DrugGraphRepository or None
        candidates:      Drug ORM 对象列表
        slots:           consult_slots

    Returns:
        需要排除的药品 generic_name 列表
    """
    if drug_graph_repo is None:
        return []

    user_conditions = slots.get("chronic_conditions", []) or []
    special_population = slots.get("special_population")
    allergies = slots.get("allergies", []) or []

    # 三个维度都没有数据 → 跳过
    if not user_conditions and not special_population and not allergies:
        return []

    excluded: list[str] = []
    for drug in candidates:
        try:
            result = await drug_graph_repo.check_contraindications(
                drug_name=drug.generic_name,
                user_conditions=user_conditions,
                special_population=special_population,
                allergies=allergies,
            )
            if result.has_contraindication:
                excluded.append(drug.generic_name)
                logger.info(
                    "KG excluded %s: conditions=%s, populations=%s, allergens=%s",
                    drug.generic_name,
                    result.matched_conditions,
                    result.matched_populations,
                    result.matched_allergens,
                )
        except Exception:
            pass  # 单药查询失败不影响整体流程

    return excluded


async def _fetch_candidates(
    drug_graph_repo,
    drug_repo: DrugRepository,
    symptom_weights: list[dict],
    symptom_names: list[str],
    category: str,
) -> list:
    """通过 Neo4j 知识图谱查询候选药物。

     Args:
         drug_graph_repo: DrugGraphRepository 实例（图数据库仓库）
         drug_repo:       PG DrugRepository 实例（仅用于获取 Drug ORM 对象的元数据）
         symptom_weights: 症状权重列表，格式为 [{name, weight}, ...]，用于 Neo4j 评分计算
         symptom_names:   纯症状名称列表（当前未使用，保留仅为向后兼容）
         category:        药物分类过滤条件

     Returns:
         Drug ORM 对象列表，按知识图谱相关性评分降序排列。
         若未找到匹配项，则返回空列表。
    """
    kg_candidates = await drug_graph_repo.find_candidates_by_symptoms(
        symptoms=symptom_weights,
        categories=[category],
    )

    if not kg_candidates:
        logger.warning(
            "KG returned no candidates for symptoms=%s category=%s",
            symptom_weights, category,
        )
        return []

    # 构建"药物名称 → KG 数据"映射字典，桥接 ScoringPipeline
    kg_score_map = {c.generic_name: c.score for c in kg_candidates}
    kg_matched_map = {c.generic_name: c.matched_symptom_count for c in kg_candidates}
    kg_total_treats_map = {c.generic_name: c.drug_total_treats for c in kg_candidates}
    kg_names = [c.generic_name for c in kg_candidates]

    # 从 PG 补全 Drug ORM 元数据
    drugs_map = {d.generic_name: d for d in await drug_repo.find_by_ids_names(kg_names)}

    # 保持 KG 排序，附加临时属性供证据规则读取：
    #   _graph_score          → GraphRelevanceScore 使用
    #   _graph_matched_count  → SymptomFocusRatio 分子
    #   _graph_total_treats   → SymptomFocusRatio 分母
    result = []
    for name in kg_names:
        drug = drugs_map.get(name)
        if drug:
            drug._graph_score = kg_score_map.get(name)
            drug._graph_matched_count = kg_matched_map.get(name)
            drug._graph_total_treats = kg_total_treats_map.get(name)
            result.append(drug)
        else:
            logger.warning("KG drug '%s' not found in PG, skipping", name)

    return result


# ── LLM 推荐回复 System Prompt ──────────────────────────

RECOMMEND_SYSTEM_PROMPT = """\
你是 OTC 药店执业药师。系统已为你准备好推荐药品的详细信息，你的任务是：
1. 为每个药品撰写具体的推荐理由（2-3句话，必须结合顾客症状 + 药品真实适应症）
2. 生成一段自然、专业、流畅的推荐回复

## 推荐理由要求（重要）
- 必须结合用户的症状（如"干咳"）和药品的真实适应症来写，说明"为什么这个药适合你"
- 格式示例："您提到干咳，右美沙芬是中枢性镇咳药，专门针对无痰干咳，效果明确且不成瘾"
- **绝对禁止**使用"适用于缓解相关症状""对症治疗""针对您的症状"等万能模板——这等于没写理由
- 如果数据不足以写出具体理由，应写"本药品主要用于..."而非编造

## 输出规范
你不是在复述说明书。你是在向普通 OTC 用户解释："这个药为什么适合他"。
请遵循：
    -1. 优先解释"为什么推荐"，而不是罗列说明书字段。
    -2. **绝对禁止**输出以下数据库标签：
        【药品名称】【作用类别】【适应症】【不良反应】【药物相互作用】
    -3. 不要重复药品全称超过一次。
    -4. 不要重复：商品名 / 英文名 / 成分类别，除非用户明确询问。
    -5. 用自然语言总结，而不是粘贴说明书原文。
    -6. "作用功效"限制在 1~2 句话。
    -7. "注意事项"只保留：与当前用户场景强相关的风险。
    -8. 不要输出与当前症状无关的信息。
    -9. 避免医学百科式解释。
    -10. 不要使用【】或类似的数据库标签包裹任何文本。

## 核心约束
- 所有药品信息必须来自系统提供的数据，不得编造任何功效、用法、剂量或禁忌
- 数据中为空的字段直接跳过，不编造内容填补
- 不要提及"评分""排名""score"等内部排序指标
- 不要生成法律免责声明（系统会自动追加）

## 回复结构
按以下层次自然组织，根据场景灵活调整，避免机械套用模板：

**开头**（1-2句过渡）：
- 首次推荐："根据您的情况，为您推荐以下药品："
- 换药/不满意："为您更换以下备选方案："
- 结合顾客主诉症状自然过渡

**药品介绍**（每个药品用 ## 标题）：
- ## 药品名
- 推荐理由（2-3句话，结合症状和适应症解释为什么推荐）
- 作用功效（1-2句自然描述，不要用标签）
- 用法用量（如有，注意年龄/人群差异）
- 注意事项（如有禁忌或重要警示，以"注意"开头）

**结尾**（1-2句即可）：
- 简短温馨提示，如"如果症状持续不缓解或加重，建议及时就医"
- 不要长篇大论

"""


def _prepare_drug_data(
    top_drugs: list,
    top_candidates: list,
    rag_map: dict[int, list[Chunk]],
    slots: dict,
) -> list[dict]:
    """提取每个药品的结构化信息，作为 LLM 输入数据。"""
    drug_data = []
    for i, sd in enumerate(top_drugs, 1):
        drug = next((d for d in top_candidates if d.id == sd.drug_id), None)
        chunks = rag_map.get(sd.drug_id, [])

        drug_data.append({
            "rank": i,
            "drug_id": sd.drug_id,
            "generic_name": sd.generic_name,
            "brand_names": drug.brand_names if drug else [],
            "otc_type": drug.otc_type if drug else "",
            "indication": drug.indication_summary if drug else "",
            "efficacy": _extract_efficacy(chunks, drug),
            "usage": _extract_usage(drug, slots, chunks),
            "warnings": _extract_warnings(chunks),
        })
    return drug_data


async def _generate_recommend_response(
    llm_client: LLMClient,
    drug_data: list[dict],
    summary: str,
) -> RecommendOutput:
    """一次 LLM 调用生成推荐理由和完整回复文本。

    Args:
        llm_client: LLM 客户端
        drug_data: 每个药品的结构化数据（efficacy/usage/warnings 已提取）
        summary:   用户症状摘要

    Returns:
        RecommendOutput: 推荐理由 + 回复文本（含免责声明）。
        LLM 失败时降级为模板拼接。
    """
    user_message = json.dumps(
        {"summary": summary, "drugs": drug_data},
        ensure_ascii=False,
        indent=2,
    )

    try:
        output = await llm_client.generate_structured(
            messages=[
                {"role": "system", "content": RECOMMEND_SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            schema=RecommendOutput,
            temperature=0.4,
            max_tokens=2048,
        )
        # 追加免责声明（LLM 不生成，系统保证合规措辞）
        output.response = output.response + DISCLAIMER
        return output

    except Exception as e:
        logger.warning(
            "LLM recommend response failed: %s, falling back to template", e
        )
        # ── 降级：用模板从 drug_data 拼回复 ──
        lines = ["根据您的情况，为您推荐以下药品：\n"]
        for d in drug_data:
            brands = f"（{'、'.join(d['brand_names'])}）" if d["brand_names"] else ""
            lines.append(f"### {d['rank']}. **{d['generic_name']}**{brands}")
            lines.append(f"**推荐理由**：适用于缓解相关症状\n")
            if d["efficacy"]:
                lines.append(f"**作用功效**：{d['efficacy']}\n")
            if d["usage"]:
                lines.append(f"**用法用量**：{d['usage']}\n")
            if d["warnings"]:
                lines.append(f"**⚠️ 注意**：{d['warnings']}\n")
            lines.append("")
        fallback_response = "\n".join(lines) + DISCLAIMER

        return RecommendOutput(
            drugs=[
                DrugReasonItem(
                    drug_id=d["drug_id"],
                    generic_name=d["generic_name"],
                    match_reason="适用于缓解相关症状",
                )
                for d in drug_data
            ],
            response=fallback_response,
        )


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


def _extract_efficacy(chunks: list[Chunk], drug) -> str:
    """提取药品功效说明，利用 chunk.section 精确过滤非功效信息。

    排除的 section：禁忌、不良反应、药物相互作用、注意事项。
    优先取高相似度的非排除 section chunk。
    降级策略：DB indication_summary → 空字符串（宁缺毋滥）。
    """
    EXCLUDED = frozenset({"禁忌", "不良反应", "药物相互作用", "注意事项"})

    if chunks:
        sorted_chunks = sorted(chunks, key=lambda c: c.score, reverse=True)
        # 优先：取第一个 section 不在排除列表的 chunk
        for chunk in sorted_chunks:
            if chunk.section not in EXCLUDED and len(chunk.content.strip()) > 15:
                return _truncate(chunk.content, 200)
        # 降级：全部被排除 → 取第一个非空 chunk（宽容处理）
        for chunk in sorted_chunks:
            if len(chunk.content.strip()) > 15:
                return _truncate(chunk.content, 200)
    # DB 兜底
    if drug and drug.indication_summary:
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
    """提取药品注意事项，优先用 section 过滤，排除已用于 efficacy 的 chunk。

    返回自然语言拼接的警告文本（最多 150 字），去重、完整句。
    """
    if not chunks:
        return ""
    # 用 section 精确匹配，降级到内容关键词匹配
    WARNING_SECTIONS = frozenset({"禁忌", "注意事项", "不良反应"})
    WARNING_KW = ("禁用", "慎用", "避免", "注意", "不宜", "禁止")

    warning_chunks = [c for c in chunks if c.section in WARNING_SECTIONS]
    if not warning_chunks:
        warning_chunks = [c for c in chunks if any(kw in c.content for kw in WARNING_KW)]

    seen: set[str] = set()
    parts: list[str] = []
    for chunk in warning_chunks[:3]:
        text = chunk.content.strip()
        # 去重：相同内容不重复
        if text in seen:
            continue
        seen.add(text)
        # 取完整的第一句
        sentences = text.replace("\n", " ").split("。")
        first = sentences[0].strip()
        if first and len(first) > 5:
            # 去掉残留的数据库标签记号【xxx】
            for tag in ("【药物相互作用】", "【不良反应】", "【禁忌】", "【注意事项】"):
                first = first.replace(tag, "")
            first = first.strip()
            if first and first not in parts:
                parts.append(first)
        if len(parts) >= 2:
            break

    if not parts:
        return ""
    return "；".join(parts) + "。"


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len].rsplit("。", 1)[0] + "。"
