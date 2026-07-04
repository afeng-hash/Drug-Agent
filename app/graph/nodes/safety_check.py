"""
SafetyCheck node — 安全规则引擎的 Graph 节点包装。

将 consult_slots 和候选药品列表传给 RuleEngine，
执行两阶段筛查（BLOCK → FILTER），返回安全结论。

增强：通过 Neo4j 知识图谱查询禁忌数据，补充规则引擎的判断。
"""

import logging

from app.graph.state import ConversationState
from app.rules.engine import RuleEngine

logger = logging.getLogger(__name__)


async def safety_check_node(
    state: ConversationState,
    rule_engine: RuleEngine,
    drug_graph_repo=None,
) -> dict:
    """运行安全规则引擎，检查当前症状 + 候选药品。

    两阶段逻辑（在 RuleEngine.check() 内部）：
      1. BLOCK 阶段：任何规则触发即短路线，直接标记 BLOCK
      2. FILTER 阶段：聚合所有排除列表，排除有风险的药品

    增强：在调用规则引擎前，通过 Neo4j 查询每个候选药品的禁忌数据，
         与用户自报的 chronic_conditions / special_population / allergies
         交叉比对，补充 graph-derived 的排除项。

    Args:
        state:          当前会话状态
        rule_engine:    安全规则引擎（已注册所有规则）
        drug_graph_repo: Neo4j 图谱仓库（可选，None 时跳过图查询）

    Returns:
        state 更新 dict：
          - safety_result → {verdict, triggered_rules, excluded_drugs, message}
          - response      → BLOCK 时设为警告文案
          - node_events   → 节点事件日志
    """
    slots = state.get("consult_slots", {})

    # ── 提取用户禁忌维度 ──
    user_conditions = slots.get("chronic_conditions", []) or []
    special_population = slots.get("special_population")
    allergies = slots.get("allergies", []) or []

    # 从推荐结果中提取候选药品名（此时可能为空，规则仍可基于症状做判断）
    candidate_drugs = [
        r.get("generic_name", "") for r in state.get("recommendations", [])
    ]

    # ── Neo4j 图谱禁忌查询 ──
    graph_excluded_drugs: list[str] = []
    graph_triggered_rules: list[dict] = []
    if drug_graph_repo is not None:
        try:
            for drug_name in candidate_drugs:
                result = await drug_graph_repo.check_contraindications(
                    drug_name=drug_name,
                    user_conditions=user_conditions,
                    special_population=special_population,
                    allergies=allergies,
                )
                if result.has_contraindication:
                    graph_excluded_drugs.append(drug_name)
                    details = []
                    if result.matched_conditions:
                        details.append(f"禁忌病症: {', '.join(result.matched_conditions)}")
                    if result.matched_populations:
                        details.append(f"禁忌人群: {', '.join(result.matched_populations)}")
                    if result.matched_allergens:
                        details.append(f"过敏成分: {', '.join(result.matched_allergens)}")
                    graph_triggered_rules.append({
                        "rule_id": "KG_CONTRAINDICATION",
                        "action": "FILTER",
                        "reason": f"{drug_name}: {'; '.join(details)}",
                    })
        except Exception:
            pass  # 图查询失败不影响规则引擎执行

    # ── 执行规则引擎 ──
    result = rule_engine.check(slots, candidate_drugs)

    # ── 合并图排除项 ──
    for drug in graph_excluded_drugs:
        if drug not in result.excluded_drugs:
            result.excluded_drugs.append(drug)
    result.triggered_rules.extend(graph_triggered_rules)

    if result.excluded_drugs and result.verdict == "PASS":
        result.verdict = "FILTER"

    safety_result = {
        "verdict": result.verdict,                         # PASS / BLOCK / FILTER
        "triggered_rules": result.triggered_rules,         # 触发了哪些规则
        "excluded_drugs": result.excluded_drugs,           # 被排除的药品名
        "message": result.message,                         # BLOCK 时的警告文案
    }

    # BLOCK 时覆盖 response 为警告消息
    response = state.get("response", "")
    if result.verdict == "BLOCK":
        response = result.message

    return {
        "safety_result": safety_result,
        "response": response,
        "node_events": [{
            "node": "safety_check",
            "verdict": result.verdict,
            "triggered_rules": [r["rule_id"] for r in result.triggered_rules],
        }],
    }
