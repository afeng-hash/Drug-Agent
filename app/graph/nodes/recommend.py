"""Recommend node — match symptoms to OTC drugs and generate recommendations."""

import json

from pydantic import BaseModel, Field

from app.db.repositories.drug import DrugRepository
from app.llm.client import LLMClient


class RecommendationItem(BaseModel):
    drug_id: int
    generic_name: str
    match_reason: str
    score: float = Field(ge=0.0, le=1.0)


class RecommendOutput(BaseModel):
    recommendations: list[RecommendationItem] = Field(max_length=3)


DISCLAIMER = (
    "\n\n---\n"
    "📋 **免责声明**：本系统仅为辅助参考，请仔细阅读说明书并按说明使用，"
    "或在药师指导下购买和使用。如症状持续或加重，请及时就医。"
)


async def recommend_node(
    state: dict,
    llm_client: LLMClient,
    drug_repo: DrugRepository,
) -> dict:
    """Match symptoms to OTC drugs and generate top 1-3 recommendations.

    Args:
        state: Current ConversationState.
        llm_client: Injected LLM client.
        drug_repo: Injected DrugRepository.

    Returns:
        State updates including recommendations, response, phase.
    """
    slots = state.get("consult_slots", {})
    summary = state.get("consult_summary", "")
    safety_result = state.get("safety_result") or {}
    excluded_drugs = safety_result.get("excluded_drugs", [])

    # Extract symptom names for DB query
    symptoms = slots.get("symptoms", [])
    symptom_names = [
        s.get("name", s) if isinstance(s, dict) else str(s)
        for s in symptoms
    ]

    # Query drugs from DB
    drugs = await drug_repo.find_by_symptoms(symptom_names, category="感冒退烧")
    if not drugs:
        drugs = await drug_repo.list_all(category="感冒退烧")

    # Filter out excluded drugs (from safety rules)
    candidates = [d for d in drugs if d.generic_name not in excluded_drugs]
    if not candidates:
        # All drugs excluded — this shouldn't normally happen
        return {
            "recommendations": [],
            "response": "抱歉，根据您的情况，目前没有合适的OTC药品推荐。建议您咨询药师或就医。",
            "phase": "ended",
            "node_events": [{"node": "recommend", "count": 0}],
        }

    # Build LLM prompt for ranking
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
        f"请从中选择 1-3 个最适合的药品，按推荐度从高到低排序。"
        f"每个药品给出一句通俗的推荐理由（基于症状匹配）。\n"
        f"score 表示推荐度（0-1），最高推荐度为 1.0。"
    )

    try:
        output = await llm_client.generate_structured(
            messages=[
                {"role": "system", "content": "你是OTC药品推荐专家，根据症状匹配最合适的药品。只输出JSON。"},
                {"role": "user", "content": prompt},
            ],
            schema=RecommendOutput,
            temperature=0.3,
            max_tokens=1024,
        )
    except Exception:
        # Fallback: return top 3 candidates sorted by name
        output = RecommendOutput(
            recommendations=[
                RecommendationItem(
                    drug_id=d.id,
                    generic_name=d.generic_name,
                    match_reason=f"适用于缓解相关症状",
                    score=0.5,
                )
                for d in candidates[:3]
            ]
        )

    recommendations = [
        {
            "drug_id": r.drug_id,
            "generic_name": r.generic_name,
            "match_reason": r.match_reason,
            "score": r.score,
        }
        for r in output.recommendations
    ]

    # Build response
    lines = ["根据您的情况，为您推荐以下药品：\n"]
    for i, r in enumerate(recommendations, 1):
        # Find brand names
        drug = next((d for d in candidates if d.id == r["drug_id"]), None)
        brands = f"（{'、'.join(drug.brand_names)}）" if drug and drug.brand_names else ""
        lines.append(f"{i}. **{r['generic_name']}**{brands}")
        lines.append(f"   {r['match_reason']}\n")

    response = "\n".join(lines) + DISCLAIMER

    return {
        "recommendations": recommendations,
        "response": response,
        "phase": "recommending",
        "node_events": [{"node": "recommend", "count": len(recommendations)}],
    }
