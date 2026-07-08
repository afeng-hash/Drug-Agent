"""
SafetyBlock node — 症状级别的安全拦截。

在推荐药品之前，仅根据用户的症状信息判断是否存在需要立即就医的危险信号。
不涉及药品级别的禁忌过滤——那一部分在 recommend_node 中通过 Neo4j + Scorer 完成。

与 recommend_node 的分工：
  safety_block  → BLOCK：高烧/婴儿/孕妇/紧急症状/严重过敏 → 立即就医，不进入推荐
  recommend_node → FILTER：Neo4j 禁忌查询 + Scorer safety 特征 → 排除不安全药品
"""

import logging

from app.api.routes.stream_events import push_step, push_text_chunked
from app.graph.state import ConversationState
from app.rules.engine import RuleEngine

logger = logging.getLogger(__name__)


async def safety_block_node(
    state: ConversationState,
    rule_engine: RuleEngine,
) -> dict:
    """症状级别的安全拦截（BLOCK only）+ consult 回复的门控推送。

    safety_block 现在位于 consult → safety_block 的固定路径上。
    consult 生成的回复文本在此处根据安全裁决决定是否推送：
      - BLOCK → 推送安全警告（覆盖 consult 回复）
      - PASS  → 推送 consult 原始回复

    规则：
      R1: 高烧 > 39°C 持续 3 天
      R2: 3 个月以下婴儿发热
      R3: 孕妇发热 > 38.5°C
      R4: 紧急症状（胸痛、呼吸困难、意识模糊等）
      R5: 严重过敏史（过敏性休克）

    触发任一规则 → verdict=BLOCK，直接终止推荐流程。

    药品禁忌过滤已移到 recommend_node 的 Neo4j 图谱查询。

    Args:
        state:       当前会话状态
        rule_engine: 安全规则引擎（已注册所有规则）

    Returns:
        state 更新 dict：
          - safety_result → {verdict, triggered_rules, message}
          - response      → BLOCK 时覆盖为警告文案
          - node_events   → 节点事件日志
    """
    slots = state.get("consult_slots", {})
    q = state.get("_event_queue")

    result = rule_engine.check(slots)

    safety_result = {
        "verdict": result.verdict,
        "triggered_rules": result.triggered_rules,
        "message": result.message,
    }

    consult_response = state.get("response", "")

    if result.verdict == "BLOCK":
        # ── 推送安全警告（覆盖 consult 回复） ──
        await push_step(
            q, "safety_block", "blocked",
            f"安全拦截: {', '.join(r['rule_id'] for r in result.triggered_rules)}",
            {"verdict": "BLOCK", "triggered_rules": result.triggered_rules},
        )
        await push_text_chunked(q, result.message, chunk_size=5, delay=0.02)
        response = result.message
    else:
        # ── PASS: 推送 consult 生成的原始回复 ──
        await push_step(
            q, "safety_block", "passed",
            "安全筛查通过",
            {"verdict": "PASS"},
        )
        if consult_response:
            await push_text_chunked(q, consult_response, chunk_size=5, delay=0.02)
        response = consult_response

    return {
        "safety_result": safety_result,
        "response": response,
        "node_events": [{
            "node": "safety_block",
            "verdict": result.verdict,
            "triggered_rules": [r["rule_id"] for r in result.triggered_rules],
        }],
    }
