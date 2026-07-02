"""
SafetyCheck node — 安全规则引擎的 Graph 节点包装。

将 consult_slots 和候选药品列表传给 RuleEngine，
执行两阶段筛查（BLOCK → FILTER），返回安全结论。
"""

from app.graph.state import ConversationState
from app.rules.engine import RuleEngine


async def safety_check_node(
    state: ConversationState,
    rule_engine: RuleEngine,
) -> dict:
    """运行安全规则引擎，检查当前症状 + 候选药品。

    两阶段逻辑（在 RuleEngine.check() 内部）：
      1. BLOCK 阶段：任何规则触发即短路线，直接标记 BLOCK
      2. FILTER 阶段：聚合所有排除列表，排除有风险的药品

    Args:
        state:       当前会话状态
        rule_engine: 安全规则引擎（已注册所有规则）

    Returns:
        state 更新 dict：
          - safety_result → {verdict, triggered_rules, excluded_drugs, message}
          - response      → BLOCK 时设为警告文案
          - node_events   → 节点事件日志
    """
    slots = state.get("consult_slots", {})

    # 从推荐结果中提取候选药品名（此时可能为空，规则仍可基于症状做判断）
    candidate_drugs = [
        r.get("generic_name", "") for r in state.get("recommendations", [])
    ]

    # 执行规则引擎
    result = rule_engine.check(slots, candidate_drugs)

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
