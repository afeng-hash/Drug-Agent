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
from app.api.routes.stream_events import push_step
from app.graph.state import ConversationState, normalize_messages
from app.llm.client import LLMClient


def _extract_last_assistant_question(messages: list) -> str:
    """从对话历史中提取系统最近一次提问的内容。

    帮助 LLM 理解"用户当前在回答什么"，尤其在否定回答（"没有"）
    和简短回答（"38度"）场景下至关重要。

    Args:
        messages: 对话历史列表（混合 dict / LangChain 对象）

    Returns:
        最近一条 assistant 消息内容，或空字符串
    """
    normalized = normalize_messages(messages)
    for m in reversed(normalized):
        if m.get("role") == "assistant":
            return m.get("content", "").strip()
    return ""


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
    dispatcher_result = state.get("dispatcher_result", {})
    #todo
    dispatcher_params = dispatcher_result.get("params", {})
    dispatcher_intent = dispatcher_result.get("intent", "")
    consult_rounds = state.get("consult_rounds", 0)
    q = state.get("_event_queue")

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

    # 提取上一轮系统提问（帮助 LLM 理解用户当前在回答什么）
    last_question = _extract_last_assistant_question(messages)

    # ── 推送 step: 开始收集 ──
    new_round = consult_rounds + 1
    await push_step(
        q, "consult", "collecting",
        f"正在收集症状信息 (第 {new_round}/{max_rounds} 轮)",
    )

    # 委托给 Consult Agent（核心逻辑在 app/agent/consult_agent.py）
    result = await run_consult(
        llm_client=llm_client,
        messages=messages,
        current_slots=slots,
        max_rounds=max_rounds,
        consult_rounds=consult_rounds,
        dispatcher_intent=dispatcher_intent,
        dispatcher_params=dispatcher_params,
        last_question=last_question,
    )

    # ── 推送 step: 槽位更新/完成 ──
    if result.next_action == "done":
        await push_step(
            q, "consult", "done",
            f"症状信息收集完成 ✓",
            {"summary": result.summary},
        )
    else:
        # 提取新增的槽位信息
        new_slots_summary = _diff_slots(slots, result.updated_slots)
        if new_slots_summary:
            await push_step(
                q, "consult", "slot_update",
                f"已确认: {new_slots_summary}",
                {"new_slots": _new_slots_dict(slots, result.updated_slots)},
            )

    # ── 将 response 存入 state，推送延迟到 safety_block（避免紧急症状追问文本先于警告输出） ──
    await push_step(
        q, "consult", "response_ready",
        f"回复已生成 ({'done' if result.next_action == 'done' else 'ask'})",
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


def _diff_slots(old: dict, new: dict) -> str:
    """对比新旧槽位，生成人类可读的变化摘要。"""
    parts = []
    for key in ("symptoms", "temperature", "duration_days", "age",
                "special_population", "chronic_conditions", "allergies",
                "medications_taken"):
        old_val = old.get(key)
        new_val = new.get(key)
        if _is_meaningful_change(old_val, new_val):
            label = {
                "symptoms": "症状", "temperature": "体温",
                "duration_days": "持续", "age": "年龄",
                "special_population": "特殊人群", "chronic_conditions": "慢性病",
                "allergies": "过敏史", "medications_taken": "已用药",
            }.get(key, key)
            parts.append(f"{label}={_format_val(new_val)}")
    return "; ".join(parts) if parts else ""


def _new_slots_dict(old: dict, new: dict) -> dict:
    """提取新增/变更的槽位（不在旧值中的部分）。"""
    changed = {}
    for key, new_val in new.items():
        old_val = old.get(key)
        if _is_meaningful_change(old_val, new_val):
            changed[key] = new_val
    return changed


def _is_meaningful_change(old_val, new_val) -> bool:
    """判断槽位值是否有意义的变化。"""
    if new_val is None:
        return False
    if isinstance(new_val, list) and len(new_val) == 0:
        return False
    if old_val == new_val:
        return False
    # 空列表 → 非空列表 = 有意义
    if isinstance(old_val, list) and len(old_val) == 0 and isinstance(new_val, list) and len(new_val) > 0:
        return True
    # None → 非空 = 有意义
    if old_val is None and new_val is not None:
        return True
    return False


def _format_val(val) -> str:
    """格式化槽位值为可读字符串。"""
    if isinstance(val, list):
        return ", ".join(
            v.get("name", str(v)) if isinstance(v, dict) else str(v)
            for v in val
        )
    if isinstance(val, float):
        return f"{val}°C"
    return str(val)
