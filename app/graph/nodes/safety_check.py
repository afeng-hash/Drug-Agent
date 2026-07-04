"""
SafetyBlock node — 症状级别的安全拦截。

在推荐药品之前，仅根据用户的症状信息判断是否存在需要立即就医的危险信号。
不涉及药品级别的禁忌过滤——那一部分在 recommend_node 中通过 Neo4j + Scorer 完成。

与 recommend_node 的分工：
  safety_block  → BLOCK：高烧/婴儿/孕妇/紧急症状/严重过敏 → 立即就医，不进入推荐
  recommend_node → FILTER：Neo4j 禁忌查询 + Scorer safety 特征 → 排除不安全药品
"""

import logging

from app.graph.state import ConversationState
from app.rules.engine import RuleEngine

logger = logging.getLogger(__name__)


async def safety_block_node(
    state: ConversationState,
    rule_engine: RuleEngine,
) -> dict:
    """症状级别的安全拦截（BLOCK only）。

    只执行 RuleEngine 的 BLOCK 阶段：
      R1: 高烧 > 39°C 持续 3 天
      R2: 3 个月以下婴儿发热
      R3: 孕妇发热 > 38.5°C
      R4: 紧急症状（胸痛、呼吸困难、意识模糊等）
      R5: 严重过敏史（过敏性休克）

    触发任一规则 → verdict=BLOCK，直接终止推荐流程。

    不执行 FILTER 阶段——药品禁忌过滤已移到 recommend_node。

    Args:
        state:       当前会话状态
        rule_engine: 安全规则引擎（已注册所有规则）

    Returns:
        state 更新 dict：
          - safety_result → {verdict, triggered_rules, message}
          - response      → BLOCK 时设为警告文案
          - node_events   → 节点事件日志
    """
    slots = state.get("consult_slots", {})

    # 不传 candidate_drugs —— BLOCK 阶段不需要药品列表
    # （FILTER 阶段需要药品列表的规则已移到 recommend_node）
    result = rule_engine.check(slots, drug_names=[])

    safety_result = {
        "verdict": result.verdict,
        "triggered_rules": result.triggered_rules,
        "excluded_drugs": [],
        "message": result.message,
    }

    response = state.get("response", "")
    if result.verdict == "BLOCK":
        response = result.message

    return {
        "safety_result": safety_result,
        "response": response,
        "node_events": [{
            "node": "safety_block",
            "verdict": result.verdict,
            "triggered_rules": [r["rule_id"] for r in result.triggered_rules],
        }],
    }
