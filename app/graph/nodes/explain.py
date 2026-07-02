"""
Explain node — RAG 驱动的药品信息查询与解释。

当用户在对话中问某个药品（如"布洛芬怎么吃""对乙酰氨基酚有副作用吗"），
Dispatcher 路由到此节点。它从数据库获取药品基本信息，从 Milvus 向量检索
说明书片段，然后交给 LLM 格式化为通俗易懂的解释。
"""

from app.agent.prompts import EXPLAIN_PROMPT
from app.db.repositories.drug import DrugRepository
from app.llm.client import LLMClient
from app.rag.retriever import DrugManualRetriever


async def explain_node(
    state: dict,
    llm_client: LLMClient,
    drug_repo: DrugRepository,
    retriever: DrugManualRetriever,
) -> dict:
    """查询药品详情并生成解释回复。

    数据来源：
      - PostgreSQL drugs 表 → 基本信息（适应症、用法用量、剂型、规格等）
      - Milvus 向量库     → 说明书片段（不良反应、禁忌、相互作用等）

    Args:
        state:     当前对话状态
        llm_client: LLM 客户端
        drug_repo:  药品仓库（已绑定 DB session）
        retriever:  Milvus 药品说明书检索器

    Returns:
        state 更新 dict：
          - response     → 完整的药品解释回复（Markdown 格式）
          - node_events  → 节点事件日志
    """
    # ── 1. 取出 Dispatcher 解析的药品名 ──
    params = state.get("dispatcher_result", {}).get("params", {})
    drug_name = params.get("drug_name", "").strip()

    if not drug_name:
        return {
            "response": "请问您想了解哪种药品的信息？",
            "node_events": [{"node": "explain", "status": "no_drug_name"}],
        }

    # ── 2. 从数据库查询药品基本信息 ──
    drug = await drug_repo.find_by_name(drug_name)
    if not drug:
        # 精确匹配失败 → 尝试模糊匹配（用户可能用商品名，如"芬必得"）
        all_drugs = await drug_repo.list_all()
        for d in all_drugs:
            if drug_name in d.brand_names or drug_name in d.generic_name:
                drug = d
                break

    # 格式化数据库信息为文本
    db_info = ""
    if drug:
        db_info = (
            f"药品名称: {drug.generic_name}\n"
            f"商品名: {'、'.join(drug.brand_names) if drug.brand_names else '暂无'}\n"
            f"作用类别: {drug.category}\n"
            f"剂型: {drug.dosage_form}\n"
            f"规格: {drug.strength}\n"
            f"适应症: {drug.indication_summary}\n"
            f"成人用法: {drug.usage_adult}\n"
            f"儿童用法: {drug.usage_child or '暂无'}\n"
            f"老人用法: {drug.usage_elderly or '暂无'}\n"
        )

    # ── 3. 从 Milvus 检索说明书片段 ──
    # 用药品通用名做过滤，用多个关键词做混合语义搜索
    search_drug_name = drug.generic_name if drug else drug_name
    chunks = []
    try:
        chunks = await retriever.retrieve(
            drug_name=search_drug_name,
            query="不良反应 禁忌 注意事项 药物相互作用 用法用量",
            top_k=5,
        )
    except Exception:
        pass  # Milvus 不可用时不阻塞，降级为仅 DB 信息

    # 格式化 RAG 检索结果为文本
    rag_context = ""
    if chunks:
        rag_context = "\n\n".join(
            f"【{c.section}】{c.content}" for c in chunks
        )

    # ── 4. LLM 整合信息并格式化输出 ──
    prompt = (
        f"{EXPLAIN_PROMPT}\n\n"
        f"## 药品基本信息\n{db_info if db_info else '暂无数据库信息'}\n\n"
        f"## 说明书检索结果\n{rag_context if rag_context else '暂无检索结果'}"
    )

    try:
        response_text = await llm_client.generate(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=1024,
        )
        response = response_text.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not response:
            response = _fallback_explain(drug_name, drug)
    except Exception:
        response = _fallback_explain(drug_name, drug)

    return {
        "response": response,
        "node_events": [{
            "node": "explain",
            "drug_name": drug_name,
            "chunks_found": len(chunks),
        }],
    }


def _fallback_explain(drug_name: str, drug) -> str:
    """LLM 不可用时的降级解释：直接返回数据库中的基本信息。

    虽然不如 LLM 格式化的自然，但能保证基本可用。
    """
    if drug:
        return (
            f"**{drug.generic_name}**（{'、'.join(drug.brand_names)}）\n\n"
            f"**作用类别**：{drug.category}\n"
            f"**适应症**：{drug.indication_summary}\n"
            f"**用法用量**：{drug.usage_adult}\n"
            f"\n如需了解详细的不良反应和禁忌，请查看药品说明书。\n"
            f"请仔细阅读说明书并按说明使用，或在药师指导下购买和使用。"
        )
    return (
        f"未找到「{drug_name}」的详细信息。请确认药品名称是否正确，或咨询店内药师。"
    )
