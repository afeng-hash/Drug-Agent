"""
Recommend node — 症状 → OTC 药品匹配推荐。

流程:
  1. 提取症状名称（所有症状等权）
  2. 症状标准化：自由文本 → KG 标准症状名
  3. Neo4j KG 查候选药品 + 禁忌过滤
  4. ScoringPipeline 确定性评分排序
  5. RAG 检索说明书片段（并行）
  6. LLM 生成推荐回复文案（真正流式，逐 token 推送）
  7. 拼装结果写入 state（match_reason 确定性生成）

数据来源分工：
  - Neo4j KG     → 症状→药品映射 + 禁忌关系
  - PostgreSQL   → 药品元数据 + 权重配置
  - ScoringPipeline → 确定性评分排序（Evidence → Features → Weighted Score）
  - Milvus/RAG   → 药品说明书片段（保留章节标签，由 LLM 自行理解）
  - LLM          → 自然语言推荐文案（真正流式输出）
"""

import asyncio
import json
import logging

from app.api.routes.stream_events import push_step, push_text_chunked, push_token
from app.db.repositories.drug import DrugRepository
from app.db.repositories.weight_config import WeightConfigRepository
from app.llm.client import LLMClient
from app.normalizer import SymptomNormalizer
from app.rag.retriever import DrugManualRetriever
from app.rag.schemas import Chunk
from app.scorer.pipeline import ScoringPipeline
from app.scorer.engine import normalize_for_display

logger = logging.getLogger(__name__)

# ── 免责声明 ──────────────────────────────────────────────

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
        llm_client:        LLM 客户端（仅用于生成推荐文案）
        drug_repo:         药品仓库（已绑定 DB session，PG 降级备用）
        weight_repo:       权重配置仓库（已绑定 DB session）
        retriever:         Milvus 说明书检索器
        scoring_pipeline:  评分排序管线
        drug_graph_repo:   Neo4j 图谱仓库（主查询路径，None 时降级到 PG）
        vocab_source:      症状词表（VocabularySource，已加载）

    Returns:
        state 更新 dict。
    """
    q = state.get("_event_queue")
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
        await push_step(q, "recommend", "normalizing", "症状标准化中...")
        normalizer = SymptomNormalizer(vocab=vocab_source, llm_client=llm_client)
        raw_names = [sw["name"] for sw in symptom_weights]
        norm_result = await normalizer.normalize(raw_names)
        for sw, ns in zip(symptom_weights, norm_result.results):
            sw["_raw_name"] = sw["name"]   # 保留原始名（供调试）
            sw["name"] = ns.standard if ns.standard else ns.raw

        # 推送标准化结果
        mappings = [
            {"raw": ns.raw, "standard": ns.standard or ns.raw, "method": ns.method}
            for ns in norm_result.results
        ]
        matched = sum(1 for ns in norm_result.results if ns.standard)
        await push_step(
            q, "recommend", "normalized",
            f"标准化完成: {matched}/{len(raw_names)} 匹配, "
            f"方法分布: {_summarize_methods(norm_result.results)}",
            {"mappings": mappings, "llm_calls": norm_result.llm_calls},
        )
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
    await push_step(q, "recommend", "searching", "检索候选药品...")
    candidates = await _fetch_candidates(
        drug_graph_repo=drug_graph_repo,
        drug_repo=drug_repo,
        symptom_weights=symptom_weights,
        symptom_names=symptom_names,
        category="感冒退烧",
    )

    if not candidates:
        await push_step(q, "recommend", "no_results", "未找到候选药品")
        return {
            "recommendations": [],
            "response": "抱歉，根据您的情况，目前没有合适的OTC药品推荐。建议您咨询药师或就医。",
            "phase": "ended",
            "node_events": [{"node": "recommend", "count": 0}],
        }

    await push_step(
        q, "recommend", "searched",
        f"找到 {len(candidates)} 个候选药品",
        {"count": len(candidates)},
    )

    # ── 3. Neo4j 图谱禁忌过滤 ──
    await push_step(q, "recommend", "filtering", "安全筛查中...")
    kg_excluded = await _filter_by_kg_contraindications(
        drug_graph_repo=drug_graph_repo,
        candidates=candidates,
        slots=slots,
    )

    if kg_excluded:
        await push_step(
            q, "recommend", "filtered",
            f"排除 {len(kg_excluded)} 个禁忌药品: {', '.join(kg_excluded)}",
            {"excluded": kg_excluded},
        )

    candidates = [d for d in candidates if d.generic_name not in kg_excluded]
    if not candidates:
        await push_step(q, "recommend", "all_excluded", "所有候选药品均被安全规则排除")
        return {
            "recommendations": [],
            "response": "抱歉，根据您的安全筛查结果，目前没有合适的OTC药品推荐。建议您咨询药师或就医。",
            "phase": "ended",
            "node_events": [{"node": "recommend", "count": 0}],
        }

    # ── 4. ScoringPipeline 确定性评分排序 ──
    await push_step(q, "recommend", "scoring", "评分排序中...")
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
        await push_step(q, "recommend", "no_valid", "评分后无有效药品")
        return {
            "recommendations": [],
            "response": "抱歉，根据您的安全筛查结果，目前没有合适的OTC药品推荐。建议您咨询药师或就医。",
            "phase": "ended",
            "node_events": [{"node": "recommend", "count": 0}],
        }

    await push_step(
        q, "recommend", "scored",
        f"评分排序完成 ({scoring_result.config_version or 'default'}, "
        f"{scoring_result.total_time_ms:.0f}ms)",
        {
            "version": scoring_result.config_version,
            "elapsed_ms": round(scoring_result.total_time_ms, 1),
            "top_drugs": [sd.generic_name for sd in top_drugs],
        },
    )

    # ── 5. RAG 检索说明书 ──
    await push_step(q, "recommend", "rag", "检索药品说明书...")
    rag_map = await _fetch_rag_batch(retriever, top_drugs)
    rag_count = sum(1 for chunks in rag_map.values() if chunks)
    await push_step(
        q, "recommend", "rag_done",
        f"说明书检索完成: {rag_count}/{len(top_drugs)}",
    )

    # ── 6. LLM 生成推荐回复文案（真正流式！）──
    top_candidates = [d for d in candidates if d.id in {sd.drug_id for sd in top_drugs}]
    drug_data = _prepare_drug_data(top_drugs, top_candidates, rag_map)

    await push_step(q, "recommend", "generating", "正在生成推荐文案...")

    # 构建流式回调
    async def on_token(token: str) -> None:
        await push_token(q, token)

    response = await _generate_recommend_response_stream(
        llm_client, drug_data, summary, slots, on_token,
    )
    # 追加免责声明（流式推送，复用 push_text_chunked）
    await push_text_chunked(q, DISCLAIMER, chunk_size=5, delay=0.01)

    # ── 7. 拼装结果 —— match_reason 确定性生成 ──
    recommendations = []
    for sd in top_drugs:
        drug = next((d for d in top_candidates if d.id == sd.drug_id), None)
        recommendations.append({
            "drug_id": sd.drug_id,
            "generic_name": sd.generic_name,
            "match_reason": _build_match_reason(
                sd.generic_name,
                drug.indication_summary if drug else "",
            ),
            "score": sd.display_score,
        })

    return {
        "recommendations": recommendations,
        "response": response + DISCLAIMER,
        "phase": "recommending",
        "node_events": [{
            "node": "recommend",
            "count": len(recommendations),
            "config_version": scoring_result.config_version,
            "scoring_ms": scoring_result.total_time_ms,
        }],
    }


# ──────────────────────────────────────────────────────────
# 症状提取
# ──────────────────────────────────────────────────────────


def _extract_symptom_names(symptoms: list) -> list[str]:
    """从槽位 symptoms 列表提取纯文本名称。"""
    return [
        s.get("name", s) if isinstance(s, dict) else str(s)
        for s in symptoms
    ]


def _summarize_methods(results: list) -> str:
    """汇总标准化方法分布，如 'kg_exact×2, llm×1'。"""
    from collections import Counter
    c = Counter(r.method for r in results)
    return ", ".join(f"{m}×{n}" for m, n in c.most_common())


# ──────────────────────────────────────────────────────────
# KG 禁忌过滤
# ──────────────────────────────────────────────────────────


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


# ──────────────────────────────────────────────────────────
# 候选药品查询
# ──────────────────────────────────────────────────────────


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


# ──────────────────────────────────────────────────────────
# RAG 检索
# ──────────────────────────────────────────────────────────


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


# ──────────────────────────────────────────────────────────
# LLM 输入准备（DB + RAG 全量透传，不做预筛选）
# ──────────────────────────────────────────────────────────


def _prepare_drug_data(
    top_drugs: list,
    top_candidates: list,
    rag_map: dict[int, list[Chunk]],
) -> list[dict]:
    """组装 LLM 输入——DB 数据全给，RAG 数据全给。

    不做预筛选、不截断、不替 LLM 做阅读理解。
    RAG 片段保留"章节"标签，让 LLM 自行判断内容归属。
    """
    drug_data = []
    for i, sd in enumerate(top_drugs, 1):
        drug = next((d for d in top_candidates if d.id == sd.drug_id), None)
        chunks = rag_map.get(sd.drug_id, [])

        # DB 结构化信息：全量透传
        db_info = {}
        if drug:
            db_info = {
                "通用名": drug.generic_name,
                "商品名": drug.brand_names,
                "OTC类型": drug.otc_type,
                "适应症": drug.indication_summary,
                "成人用法": drug.usage_adult,
                "儿童用法": drug.usage_child or "",
                "老人用法": drug.usage_elderly or "",
            }

        # RAG 说明书片段：全量透传，保留章节标签让 LLM 自行理解
        manuals = [
            {"章节": c.section, "内容": c.content}
            for c in chunks
        ] if chunks else []

        drug_data.append({
            "排名": i,
            "结构化信息": db_info,
            "说明书片段": manuals,
        })
    return drug_data


# ──────────────────────────────────────────────────────────
# LLM 推荐回复 Prompt
# ──────────────────────────────────────────────────────────

RECOMMEND_SYSTEM_PROMPT = """\
你是 OTC 药店执业药师。基于以下药品数据，为顾客撰写推荐回复。

## 数据说明
- "结构化信息"：药品数据库字段（适应症、用法用量等）
- "说明书片段"：从药品说明书向量检索到的段落，"章节"标注了来源（如"适应症""用法用量""注意事项""禁忌""不良反应""药物相互作用"）

## 顾客信息
顾客症状、年龄、特殊人群身份等信息已标注在输入中。

## 要求
1. 为每个药品写 1-2 句具体的推荐理由——从"适应症"字段或说明书"适应症"章节取信息，结合顾客症状说明为什么推荐
2. 作用功效用 1-2 句通俗语言描述（从适应症相关章节取，不要取禁忌/药物相互作用章节的内容）
3. 用法用量如有则说明（注意根据顾客年龄和特殊人群身份选择合适的用法）
4. 如说明书"注意事项"章节有重要警示，简要提醒
5. 数据为空的字段跳过，不编造；说明书片段为空则只用结构化信息
6. 用自然语言，不要使用【】等数据库标签
7. 不要生成免责声明（系统会自动追加）

## 回复结构参考
**开头**：1-2 句过渡
**药品介绍**：每个药用 ### 标题，包含推荐理由 + 作用功效 + 用法用量 + 注意事项（如有）
**结尾**：简短温馨提示，温馨提示要加粗
"""


# ──────────────────────────────────────────────────────────
# LLM 调用 + 降级
# ──────────────────────────────────────────────────────────


async def _generate_recommend_response_stream(
    llm_client: LLMClient,
    drug_data: list[dict],
    summary: str,
    slots: dict,
    on_token: callable = None,
) -> str:
    """流式 LLM 生成推荐回复文案——真正逐 token 推送。

    将 LLM 职责从"同时产出结构化理由 + 自然语言文案"简化为
    "只产出一段自然语言文案"。推荐理由（match_reason）由
    _build_match_reason() 确定性生成。

    Args:
        llm_client: LLM 客户端
        drug_data:  每个药品的 DB + RAG 数据
        summary:    用户症状摘要
        slots:      用户 consult_slots（含年龄、特殊人群等）
        on_token:   每个 token 的回调（async callable）

    Returns:
        完整推荐回复文本（不含免责声明）。
        LLM 失败时降级为模板拼接。
    """
    # 构建顾客上下文
    customer = {"症状摘要": summary}
    age = slots.get("age")
    special_pop = slots.get("special_population")
    if age is not None:
        customer["年龄"] = age
    if special_pop:
        customer["特殊人群"] = special_pop

    user_message = json.dumps(
        {"顾客信息": customer, "推荐药品": drug_data},
        ensure_ascii=False,
        indent=2,
    )

    try:
        content = await llm_client.generate_with_stream_callback(
            messages=[
                {"role": "system", "content": RECOMMEND_SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            on_token=on_token,
            temperature=0.4,
            max_tokens=2048,
            node="recommend",
        )
        return content

    except Exception as e:
        logger.warning(
            "LLM recommend response failed: %s, falling back to template", e
        )
        fallback_text = _build_fallback_response(drug_data)
        # 降级模板也需要流式推送（通过 on_token 逐块发送）
        if on_token:
            for i in range(0, len(fallback_text), 5):
                await on_token(fallback_text[i:i + 5])
                await asyncio.sleep(0.02)
        return fallback_text


async def _generate_recommend_response(
    llm_client: LLMClient,
    drug_data: list[dict],
    summary: str,
    slots: dict,
) -> str:
    """LLM 生成推荐回复文案——非流式版本（保留向后兼容）。"""
    return await _generate_recommend_response_stream(
        llm_client, drug_data, summary, slots, on_token=None,
    )


# ──────────────────────────────────────────────────────────
# 确定性推荐理由（零 LLM 开销）
# ──────────────────────────────────────────────────────────


def _build_match_reason(generic_name: str, indication_summary: str) -> str:
    """从药品适应症确定性生成推荐理由。

    始终诚实、可溯源、零 LLM 开销。
    """
    if indication_summary:
        first_sentence = indication_summary.split("。")[0].strip()
        if len(first_sentence) > 8:
            return f"{generic_name}主要用于{first_sentence}"
    return f"{generic_name}是对症的非处方药，建议参考说明书确认是否适合您的情况"


# ──────────────────────────────────────────────────────────
# 降级：模板拼接回复
# ──────────────────────────────────────────────────────────


def _build_fallback_response(drug_data: list[dict]) -> str:
    """LLM 调用失败时，用模板从结构化数据拼接回复。"""
    lines = ["根据您的情况，为您推荐以下药品：\n"]
    for d in drug_data:
        db = d.get("结构化信息", {})
        name = db.get("通用名", "")
        indication = db.get("适应症", "")
        usage = db.get("成人用法", "")
        brands = "、".join(db.get("商品名", []))

        reason = _build_match_reason(name, indication)

        lines.append(
            f"### {d['排名']}. **{name}**"
            + (f"（{brands}）" if brands else "")
        )
        lines.append(f"**推荐理由**：{reason}\n")
        if indication:
            lines.append(f"**作用功效**：{indication}\n")
        if usage:
            lines.append(f"**用法用量**：{usage}\n")
        lines.append("")
    return "\n".join(lines)
