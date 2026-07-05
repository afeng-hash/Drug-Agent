"""
Conditional edge functions — LangGraph 路由决策。

LangGraph 的条件边（conditional edges）需要一个函数，接收当前 state，
返回目标节点名称。这三个函数就是 Dispatcher / Consult / SafetyCheck 节点的
"下一站"判断逻辑。

它们不修改 state，只读取 state 中的关键字段做决策。
"""

from app.graph.state import ConversationState


def route_after_dispatcher(state: ConversationState) -> str:
    """Dispatcher 节点之后：根据 LLM 判断的路由结果决定下一站。

    读取 dispatcher_result.route，映射到实际节点名。
    如果 LLM 返回了无效路由（不应该发生，但兜底），默认走 consult。

    可达目标：consult / explain / end
    （recommend 路由已移除——推荐永远是 consult→done 的自然结果）
    """
    route = state.get("dispatcher_result", {}).get("route", "consult")
    # 白名单校验：只允许这 3 个有效路由
    valid_routes = {"consult", "explain", "end"}
    return route if route in valid_routes else "consult"


def route_after_consult(state: ConversationState) -> str:
    """Consult 节点之后：根据问诊状态决定下一步。

    读取 consult_next_action：
      - "done" → 信息充分，进入安全筛查
      - "ask" | 其他 → 信息不够，结束本轮等待用户回复

    可达目标：safety_block / end
    """
    next_action = state.get("consult_next_action", "ask")
    if next_action == "done":
        return "safety_block"   # 症状收集完毕 → 安全拦截 → 推荐
    return "end"                # 需要继续追问 → 结束本轮，前端展示追问语


def route_after_safety(state: ConversationState) -> str:
    """safety_block 节点之后：根据安全结论决定下一步。

    读取 safety_result.verdict：
      - "BLOCK" → 存在危险信号，终止推荐，直接返回就医警告
      - "PASS"  → 安全，继续推荐流程

    可达目标：recommend / end
    """
    safety_result = state.get("safety_result") or {}
    verdict = safety_result.get("verdict", "PASS")
    if verdict == "BLOCK":
        return "end"            # 拦截 → 不进入推荐
    return "recommend"          # 通过 → 继续推荐
