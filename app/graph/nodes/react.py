"""
React node — ReactAgent 驱动的智能回复（替代旧 explain 节点）。

在 Graph 中位于两条路径：
  A. dispatcher → react → end（纯 react，跳过 workflow）
  B. consult → react → end（ask + react 混合意图）
  C. inventory → react → end（workflow done + react 混合意图）

ReactAgent 通过工具查询药品信息，并能利用 workflow 上下文做智能衔接。
"""

import logging

from app.agent.react.agent import ReactAgent
from app.graph.state import ConversationState, normalize_messages

logger = logging.getLogger(__name__)


async def react_node(
    state: ConversationState,
    react_agent: ReactAgent,
    state_proxy=None,
) -> dict:
    """执行 ReactAgent，利用 state 中的 workflow 上下文做智能衔接。

    与旧 explain 节点的区别：
      - explain: DB 查药 + RAG 检索 → LLM 模板输出（只能解释单个药）
      - react:   ReactAgent 工具驱动 → 可以查药、对比、解析指代、闲聊

    Args:
        state:        当前对话状态
        react_agent:  ReactAgent 实例（已注册 6 个工具）
        state_proxy:  _StateProxy 实例，从中读取 recommendations/user_profile 给工具

    Returns:
        state 更新 dict：response, phase, node_events
    """
    # ── 0. 统一 normalize messages（F1 修复：只 normalize 一次） ──
    raw_messages = state.get("messages", [])
    messages = normalize_messages(raw_messages)

    # ── 1. 获取 react query ──
    actions = state.get("dispatcher_result", {}).get("actions", [])
    react_actions = [a for a in actions if a.get("action") == "react"]
    query = ""
    if react_actions:
        query = react_actions[0].get("query", "")

    # 没有显式 query → 取最后一条用户消息（已 normalize，顺序正确）
    if not query:
        for m in reversed(messages):
            if m.get("role") == "user":
                query = m.get("content", "")
                break

    # ── 2. 构建 workflow 上下文（F2 修复：基于 phase + recommendations 判断） ──
    recommendations = state.get("recommendations", [])
    consult_next_action = state.get("consult_next_action", "")
    current_phase = state.get("phase", "")

    # 判定 workflow 真实完成状态
    if recommendations and current_phase in ("recommending", "ended"):
        workflow_action = "done"
    elif consult_next_action == "ask" and current_phase == "consulting":
        workflow_action = "ask"
    elif recommendations:
        # 有推荐结果但 phase 不是 recommending——可能是新 turn，按 done 处理
        workflow_action = "done"
    else:
        workflow_action = "ask"

    # 构造 workflow_response：若 response 为空但有 recommendations，自动生成摘要
    workflow_response = state.get("response", "")
    if not workflow_response and recommendations:
        drug_names = [
            r.get("generic_name", "") for r in recommendations[:3]
        ]
        workflow_response = f"系统已推荐：{'、'.join(drug_names)}"

    workflow_context = None
    if workflow_response or recommendations:
        workflow_context = {
            "workflow_action": workflow_action,
            "workflow_response": workflow_response,
        }

    # ── 3. 更新 state_proxy（工具 get_recommendation/get_user_profile 的数据源） ──
    if state_proxy is not None:
        state_proxy.recommendations = recommendations
        slots = state.get("consult_slots", {})
        state_proxy.user_profile = {
            "age": slots.get("age"),
            "allergies": slots.get("allergies", []),
            "chronic_conditions": slots.get("chronic_conditions", []),
            "special_population": slots.get("special_population"),
        }

    # ── 4. 执行 ReactAgent（传入已 normalize 的 messages，F1 修复） ──
    result = await react_agent.run(
        user_message=query,
        history=messages,
        context=workflow_context,
    )

    # ── 5. 组装最终回复 ──
    previous_response = state.get("response", "")
    if previous_response:
        final_response = f"{previous_response}\n\n{result.final_response}"
    else:
        final_response = result.final_response

    # ── 6. 判断 phase ──
    if workflow_action == "ask" and current_phase == "consulting":
        phase = "consulting"
    elif state.get("phase") == "consulting":
        phase = "consulting"
    else:
        phase = "ended"

    return {
        "response": final_response,
        "phase": phase,
        "node_events": [{
            "node": "react",
            "intent": react_actions[0].get("intent", "") if react_actions else "",
            "iterations": result.total_iterations,
            "total_time_ms": result.total_time_ms,
        }],
    }
