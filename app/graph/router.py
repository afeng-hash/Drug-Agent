"""
Graph router — 条件边路由函数（v2: 基于 actions[] 执行计划）。

每个函数接收 state，返回下一个节点的名称（str）。
LangGraph 的 add_conditional_edges 调用这些函数来决定流向。

与 v1 的区别：
  - 不再读 dispatcher_result.route（单路由），改为读 actions[]
  - 路由感知 react action 的存在，决定 workflow 之后是否走 react
"""

from app.graph.state import ConversationState


def route_after_dispatcher(state: ConversationState) -> str:
    """Dispatcher 之后的分发。

    Returns:
        "consult" — 有 workflow action，走症状求药链路
        "react"   — 无 workflow（纯 react），直接走 ReactAgent
    """
    actions = _get_actions(state)

    has_workflow = any(a.get("action") == "workflow" for a in actions)

    if has_workflow:
        return "consult"

    # 纯 react 或无计划 → react
    return "react"


def route_after_safety(state: ConversationState) -> str:
    """Safety 之后的分发。

    safety_block 现在位于 consult → safety_block → ... 的固定路径上，
    所有 consult 输出都必须经过 safety 检查。

    Returns:
        "recommend" — PASS + consult done → 进入药品推荐
        "react"     — PASS + consult ask + 有 react 待执行
        "end"       — BLOCK，或 PASS + consult ask + 无 react
    """
    safety = state.get("safety_result", {})
    if safety.get("verdict") == "BLOCK":
        return "end"

    # PASS: 根据 consult 的状态决定下一步
    next_action = state.get("consult_next_action", "ask")
    if next_action == "done":
        return "recommend"

    # ask: 如果有 react action 需要执行，走 react；否则直接结束本轮
    actions = _get_actions(state)
    has_react = any(a.get("action") == "react" for a in actions)
    return "react" if has_react else "end"


def route_after_inventory(state: ConversationState) -> str:
    """Inventory 之后的分发。

    Returns:
        "react" — 有 react action 待执行，ReactAgent 做最终回复和衔接
        "end"   — 无 react，直接结束
    """
    actions = _get_actions(state)
    has_react = any(a.get("action") == "react" for a in actions)
    return "react" if has_react else "end"


def _get_actions(state: ConversationState) -> list[dict]:
    """从 dispatcher_result 中提取 actions[]。"""
    return state.get("dispatcher_result", {}).get("actions", [])
