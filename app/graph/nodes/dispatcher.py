"""
Dispatcher node — 对话路由分发控制器。

这是整个对话系统的"大脑"：每次用户发来消息，Dispatcher 分析对话上下文和用户意图，
决定下一步应该走哪个节点（consult / explain / recommend / end）。

它不做任何业务逻辑，只做路由决策。由 LLM 驱动（调用通义千问）。
"""

import json

from pydantic import BaseModel, Field

from app.agent.prompts import DISPATCHER_PROMPT
from app.graph.state import ConversationState, normalize_messages
from app.llm.client import LLMClient


class DispatcherDecision(BaseModel):
    """LLM 输出的路由决策结构。

    通过 generate_structured() 让 LLM 严格输出 JSON，解析为此模型。
    """

    route: str = Field(
        description="目标节点: consult(症状问诊) | explain(药品解释) | recommend(直接推荐) | end(结束)"
    )
    intent: str = Field(
        description="细分的用户意图: describe_symptom | ask_drug | switch_drug | switch_symptom | give_up | other"
    )
    params: dict = Field(
        default_factory=dict,
        description="附加参数，如 {drug_name: '布洛芬', reset_slots: true, filter_cheaper: true}"
    )


async def dispatcher_node(state: ConversationState, llm_client: LLMClient) -> dict:
    """分析对话上下文 + 用户最新消息，决策路由和意图。

    处理流程：
      1. 从 state.messages 中提取最新一条用户消息
      2. 收集上下文：当前阶段、之前阶段、已收集的症状摘要
      3. 调用 LLM（结构化输出）生成路由决策
      4. 根据决策更新 previous_phase（记录跳转前的阶段）

    Args:
        state:      当前对话状态
        llm_client: LLM 客户端（注入）

    Returns:
        state 更新 dict：dispatcher_result, previous_phase, phase, node_events
    """
    # ── 1. 提取最新用户消息 ──
    messages = normalize_messages(state.get("messages", []))
    if not messages:
        return _fallback_route()  # 无消息 → 默认走 consult

    latest_user = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            latest_user = m.get("content", "")
            break

    if not latest_user.strip():
        return _fallback_route()  # 空消息 → 默认走 consult

    # ── 2. 收集 LLM 输入上下文 ──
    slots = state.get("consult_slots", {})
    current_phase = state.get("phase", "intake")
    previous_phase = state.get("previous_phase")

    context = {
        "current_phase": current_phase,
        "previous_phase": previous_phase,
        "collected_slots_summary": _summarize_slots(slots),  # 人类可读的症状摘要
        "user_message": latest_user,
    }

    prompt_messages = [
        {"role": "system", "content": DISPATCHER_PROMPT},
        {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
    ]

    # ── 3. 调用 LLM 获取路由决策 ──
    try:
        decision = await llm_client.generate_structured(
            messages=prompt_messages,
            schema=DispatcherDecision,
            temperature=0.2,   # 低温度：路由决策需要稳定，不要创意
            max_tokens=256,
        )
    except Exception:
        # LLM 调用失败 → 默认走 consult（安全兜底）
        return _fallback_route()

    # ── 4. 处理 previous_phase 跳转逻辑 ──
    new_previous_phase = previous_phase

    # 如果在问诊/初始阶段跳去解释药品 → 记录当前阶段，以便解释完回到原来的流程
    if decision.route == "explain" and current_phase in ("consulting", "intake"):
        new_previous_phase = current_phase

    # 如果用户放弃 → 清空 previous_phase（不再需要回到原来流程）
    if decision.route == "end" and decision.intent == "give_up":
        new_previous_phase = None

    return {
        "dispatcher_result": {
            "route": decision.route,
            "intent": decision.intent,
            "params": decision.params,
        },
        "previous_phase": new_previous_phase,
        "phase": current_phase,
        "node_events": [{
            "node": "dispatcher",
            "route": decision.route,
            "intent": decision.intent,
        }],
    }


def _fallback_route() -> dict:
    """LLM 失败或无法判断时的默认路由 → consult。

    安全策略：不确定时宁可多问几句，不要乱推荐。
    """
    return {
        "dispatcher_result": {
            "route": "consult",
            "intent": "fallback",
            "params": {},
        },
        "node_events": [{"node": "dispatcher", "route": "consult", "intent": "fallback"}],
    }


def _summarize_slots(slots: dict) -> str:
    """将症状槽位转为人类可读的摘要字符串，给 LLM 做上下文。

    示例输出：
      "症状: 头痛, 发烧；体温: 38.5°C；持续: 3天；年龄: 28岁；过敏史: 阿司匹林"

    如果槽位都是空的，返回 "暂无已收集的症状信息"。
    """
    if not slots:
        return "暂无已收集的症状信息"

    parts = []

    symptoms = slots.get("symptoms", [])
    if symptoms:
        names = [s.get("name", s) if isinstance(s, dict) else str(s) for s in symptoms]
        parts.append(f"症状: {', '.join(names)}")

    temp = slots.get("temperature")
    if temp is not None:
        parts.append(f"体温: {temp}°C")

    days = slots.get("duration_days")
    if days is not None:
        parts.append(f"持续: {days}天")

    pop = slots.get("special_population")
    if pop:
        parts.append(f"特殊人群: {pop}")

    age = slots.get("age")
    if age is not None:
        parts.append(f"年龄: {age}岁")

    allergies = slots.get("allergies", [])
    if allergies:
        parts.append(f"过敏史: {', '.join(allergies)}")

    return "；".join(parts) if parts else "暂无已收集的症状信息"
