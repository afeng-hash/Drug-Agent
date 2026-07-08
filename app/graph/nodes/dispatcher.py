"""
Dispatcher node — 对话意图解析器（v2: 执行计划格式）。

每次用户发来消息，Dispatcher 分析对话上下文和用户意图，
输出有序的执行计划 actions[]，告诉 Orchestrator 接下来要做什么。

从 v2 开始，Dispatcher 不再输出单一路由，而是输出执行计划：
  - workflow: 症状求药主链路（consult → safety → recommend → inventory）
  - react:    通用对话（药品查询、对比、闲聊等，由 ReactAgent 处理）

它不做任何业务逻辑，只做意图解析。由 LLM 驱动（调用通义千问）。
"""

import json

from pydantic import BaseModel, Field

from app.agent.prompts import DISPATCHER_PROMPT
from app.graph.state import ConversationState, normalize_messages
from app.llm.client import LLMClient


class ActionItem(BaseModel):
    """执行计划中的一个动作。"""

    action: str = Field(
        description="动作类型: 'workflow'（症状求药主链路） | 'react'（通用对话/药品咨询）"
    )
    intent: str = Field(
        description="意图分类。workflow: describe_symptom | answer_question | want_recommend | switch_drug；"
                    "react: ask_drug | compare_drugs | ask_interaction | check_inventory | chat | give_up"
    )
    query: str = Field(
        default="",
        description="react 动作时的用户核心问题。workflow 时可为空"
    )
    priority: int = Field(
        default=1,
        description="执行顺序，1=先执行。workflow 始终为 1，react 为 2"
    )


class DispatcherDecision(BaseModel):
    """LLM 输出的执行计划。

    包含 1-2 个有序动作。workflow 始终在 react 之前执行。
    """

    actions: list[ActionItem] = Field(
        description="有序动作列表，长度 1-2。workflow（priority=1）在 react（priority=2）之前"
    )


async def dispatcher_node(state: ConversationState, llm_client: LLMClient) -> dict:
    """分析对话上下文 + 用户最新消息，输出执行计划 actions[]。

    Dispatcher 只负责意图解析，不判断信息是否充分、不决定是否可以推荐。
    Workflow 由 Orchestrator 编排（consult → safety → recommend → inventory）,
    React 由 ReactAgent 工具驱动处理。

    处理流程：
      1. 从 state.messages 中提取最新一条用户消息
      2. 收集上下文：当前阶段、已收集的症状摘要、最近对话
      3. 调用 LLM（结构化输出）生成执行计划
      4. 输出 dispatcher_result = {actions: [...]}

    Args:
        state:      当前对话状态
        llm_client: LLM 客户端（注入）

    Returns:
        state 更新 dict：dispatcher_result, phase, node_events
    """
    from app.api.routes.stream_events import push_step

    q = state.get("_event_queue")

    # ── 1. 提取最新用户消息 ──
    messages = normalize_messages(state.get("messages", []))
    if not messages:
        return _fallback_plan()

    latest_user = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            latest_user = m.get("content", "")
            break

    if not latest_user.strip():
        return _fallback_plan()

    # ── 2. 收集 LLM 输入上下文 ──
    slots = state.get("consult_slots", {})
    current_phase = state.get("phase", "intake")

    # 取最近 N 条对话历史作为上下文（帮助 LLM 理解对话进展）
    recent_n = 8  # 最近 4 轮对话
    recent_messages = messages[-recent_n:] if len(messages) > recent_n else messages

    context = {
        "current_phase": current_phase,
        "collected_slots_summary": _summarize_slots(slots),
        "recent_conversation": _format_recent_messages(recent_messages),
        "user_message": latest_user,
    }

    prompt_messages = [
        {"role": "system", "content": DISPATCHER_PROMPT},
        {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
    ]

    # ── 3. 调用 LLM 获取执行计划 ──
    await push_step(q, "dispatcher", "analyzing", "正在分析用户意图...")
    try:
        decision = await llm_client.generate_structured(
            messages=prompt_messages,
            schema=DispatcherDecision,
            temperature=0.2,
            max_tokens=256,
            node="dispatcher",
        )
    except Exception:
        await push_step(q, "dispatcher", "fallback", "意图分析失败，使用默认计划")
        return _fallback_plan()

    # ── 4. 推送决策结果 ──
    action_summary = ", ".join(
        f"{a.action}({a.intent})" for a in decision.actions
    )
    await push_step(
        q, "dispatcher", "decided",
        f"执行计划: {action_summary}",
        {"actions": [a.model_dump() for a in decision.actions]},
    )

    # ── 5. 构建 node_events ──
    events = [{
        "node": "dispatcher",
        "actions": [
            {"action": a.action, "intent": a.intent, "priority": a.priority}
            for a in decision.actions
        ],
    }]

    return {
        "dispatcher_result": {
            "actions": [a.model_dump() for a in decision.actions],
        },
        "phase": current_phase,
        "node_events": events,
    }


def _fallback_plan() -> dict:
    """LLM 失败或无法判断时的默认执行计划 → 全部走 react（安全兜底）。

    安全策略：不确定时走 react，由 ReactAgent 通过工具查询信息。
    """
    return {
        "dispatcher_result": {
            "actions": [
                {"action": "react", "intent": "fallback", "query": "", "priority": 1}
            ],
        },
        "node_events": [
            {"node": "dispatcher", "actions": [{"action": "react", "intent": "fallback"}]}
        ],
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


def _format_recent_messages(messages: list[dict]) -> str:
    """将最近 N 条对话历史格式化为人类可读的字符串，给 LLM 做上下文。

    示例输出：
      "用户: 我头疼发烧两天了\n系统: 请问体温多少度？\n用户: 38度"

    Args:
        messages: 标准化后的消息列表 [{"role": "...", "content": "..."}]

    Returns:
        格式化的对话历史字符串
    """
    if not messages:
        return "（无对话历史）"

    lines = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "user":
            lines.append(f"用户: {content}")
        elif role == "assistant":
            lines.append(f"系统: {content}")
        # system 消息不展示（通常是 prompt）
    return "\n".join(lines) if lines else "（无对话历史）"
