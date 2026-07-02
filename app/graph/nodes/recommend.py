"""
Recommend node — 症状 → OTC 药品匹配推荐。

这是推荐环节的核心节点：从数据库查询候选药品，用 LLM 对症状进行匹配排序，
最终给出 1-3 个最合适的 OTC 药品推荐。

流程：
  1. 从 consult_slots 提取症状名称
  2. 从数据库查询匹配的药品（按 category="感冒退烧" 过滤）
  3. 排除 safe_excluded_drugs（安全规则过滤的药品）
  4. 用 LLM 对候选药品排序（1-3 个）
  5. 生成自然语言推荐回复
"""

import json

from pydantic import BaseModel, Field

from app.db.repositories.drug import DrugRepository
from app.llm.client import LLMClient


# ── LLM 结构化输出 Schema ────────────────────────────────

class RecommendationItem(BaseModel):
    """单个推荐药品。"""
    drug_id: int
    generic_name: str
    match_reason: str
    score: float = Field(ge=0.0, le=1.0)  # 推荐度 0~1，越高越推荐


class RecommendOutput(BaseModel):
    """LLM 排序输出的推荐列表，最多 3 个。"""
    recommendations: list[RecommendationItem] = Field(max_length=3)


# ── 回复模板 ──────────────────────────────────────────────

DISCLAIMER = (
    "\n\n---\n"
    "📋 **免责声明**：本系统仅为辅助参考，请仔细阅读说明书并按说明使用，"
    "或在药师指导下购买和使用。如症状持续或加重，请及时就医。"
)
"""每条推荐回复末尾都会追加此免责声明"""


async def recommend_node(
    state: dict,
    llm_client: LLMClient,
    drug_repo: DrugRepository,
) -> dict:
    """执行药品推荐。

    Args:
        state:     当前对话状态
        llm_client: LLM 客户端
        drug_repo:  药品仓库（已绑定 DB session）

    Returns:
        state 更新 dict：
          - recommendations → 推荐的药品列表（1-3 个）
          - response        → 格式化的推荐回复文本（含免责声明）
          - phase           → "recommending"
          - node_events     → 节点事件日志
    """
    slots = state.get("consult_slots", {})
    summary = state.get("consult_summary", "")
    safety_result = state.get("safety_result") or {}
    excluded_drugs = safety_result.get("excluded_drugs", [])

    # ── 1. 从槽位提取症状名称 ──
    symptoms = slots.get("symptoms", [])
    symptom_names = [
        s.get("name", s) if isinstance(s, dict) else str(s)
        for s in symptoms
    ]

    # ── 2. 从数据库查询候选药品 ──
    drugs = await drug_repo.find_by_symptoms(symptom_names, category="感冒退烧")
    if not drugs:
        # 症状匹配不到药品 → 列出所有感冒退烧类药品作为兜底
        drugs = await drug_repo.list_all(category="感冒退烧")

    # ── 3. 排除安全规则过滤的药品 ──
    # 例如用户对阿司匹林过敏 → 排除所有含阿司匹林的药品
    candidates = [d for d in drugs if d.generic_name not in excluded_drugs]
    if not candidates:
        # 所有候选药品都被安全规则排除了 → 返回抱歉消息
        return {
            "recommendations": [],
            "response": "抱歉，根据您的情况，目前没有合适的OTC药品推荐。建议您咨询药师或就医。",
            "phase": "ended",
            "node_events": [{"node": "recommend", "count": 0}],
        }

    # ── 4. LLM 排序推荐 ──
    # 把候选药品信息发给 LLM，让它挑最好的 1-3 个
    drug_list = [
        {
            "id": d.id,
            "generic_name": d.generic_name,
            "brand_names": d.brand_names,           # 商品名（如芬必得、美林）
            "indication": d.indication_summary,     # 适应症
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
        # LLM 失败 → 简单取前 3 个候选药品，score 统一 0.5
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

    # ── 5. 生成自然语言回复 ──
    lines = ["根据您的情况，为您推荐以下药品：\n"]
    for i, r in enumerate(recommendations, 1):
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
