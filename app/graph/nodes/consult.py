"""
Consult node — ReAct 症状问诊。

将对话委托给 Consult Agent（app/agent/consult_agent.py），由 LLM 执行：
  1. 分析用户新提供的信息
  2. 更新症状槽位（consult_slots）
  3. 判断信息是否充分
  4. 决定继续追问还是完成收集

本节点只是一个薄包装：处理 state 的读写，核心逻辑在 consult_agent.run_consult()。
"""

from app.agent.consult_agent import run_consult
from app.graph.state import ConversationState
from app.llm.client import LLMClient


async def consult_node(
    state: ConversationState,
    llm_client: LLMClient,
    max_rounds: int = 6,
) -> dict:
    """执行一轮症状问诊。

    Args:
        state:      当前对话状态
        llm_client: LLM 客户端（注入）
        max_rounds: 最大追问轮数。超过此数强制结束问诊，进入推荐

    Returns:
        state 更新 dict：
          - consult_slots        → 更新后的症状槽位
          - consult_next_action  → "ask"（继续追问）或 "done"（完成收集）
          - consult_summary      → done 时的症状摘要
          - response             → 回复文本（追问语或过渡语）
          - phase                → "consulting"
          - node_events          → 节点事件日志
    """
    messages = state.get("messages", [])
    slots = state.get("consult_slots", {})
    dispatcher_params = state.get("dispatcher_result", {}).get("params", {})
    consult_rounds = state.get("consult_rounds", 0)

    # 如果 Dispatcher 标记了 reset_slots（用户切换了症状描述），清空旧槽位
    reset_slots = dispatcher_params.get("reset_slots", False)
    if reset_slots:
        slots = {
            "symptoms": [],
            "temperature": None,
            "duration_days": None,
            "medications_taken": [],
            "special_population": None,
            "age": None,
            "chronic_conditions": [],
            "allergies": [],
        }
        consult_rounds = 0  # 新症状 → 轮数从头算

    # 委托给 Consult Agent（核心逻辑在 app/agent/consult_agent.py）
    result = await run_consult(
        llm_client=llm_client,
        messages=messages,
        current_slots=slots,
        max_rounds=max_rounds,
        consult_rounds=consult_rounds,
    )

    return {
        "consult_slots": result.updated_slots,
        "consult_next_action": result.next_action,  # "ask" 或 "done"
        "consult_summary": result.summary,
        "consult_rounds": consult_rounds + 1,       # 本轮追问完成，递增
        "response": result.response,
        "phase": "consulting",
        "node_events": [{
            "node": "consult",
            "next_action": result.next_action,
            "summary": result.summary,
            "round": consult_rounds + 1,
        }],
    }
