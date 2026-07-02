"""Explain node — RAG-powered drug information lookup."""

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
    """Look up drug information via DB + RAG and present a structured explanation.

    Args:
        state: Current ConversationState.
        llm_client: Injected LLM client.
        drug_repo: Injected DrugRepository.
        retriever: Injected DrugManualRetriever.

    Returns:
        State updates including response.
    """
    params = state.get("dispatcher_result", {}).get("params", {})
    drug_name = params.get("drug_name", "").strip()

    if not drug_name:
        return {
            "response": "请问您想了解哪种药品的信息？",
            "node_events": [{"node": "explain", "status": "no_drug_name"}],
        }

    # Get structured drug info from DB
    drug = await drug_repo.find_by_name(drug_name)
    if not drug:
        # Try fuzzy — maybe user asked by brand name
        all_drugs = await drug_repo.list_all()
        for d in all_drugs:
            if drug_name in d.brand_names or drug_name in d.generic_name:
                drug = d
                break

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

    # Retrieve relevant manual chunks
    search_drug_name = drug.generic_name if drug else drug_name
    chunks = []
    try:
        chunks = await retriever.retrieve(
            drug_name=search_drug_name,
            query="不良反应 禁忌 注意事项 药物相互作用 用法用量",
            top_k=5,
        )
    except Exception:
        pass  # Milvus might not be available

    rag_context = ""
    if chunks:
        rag_context = "\n\n".join(
            f"【{c.section}】{c.content}" for c in chunks
        )

    # LLM formats the explanation
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
    """Generate a basic explanation without LLM."""
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
