"""
ReAct-style consult agent — LLM 驱动的动态症状收集。

这是问诊环节的核心：每轮对话中，Consult Agent 接收对话历史和当前已收集的
症状槽位，输出更新后的槽位、追问文本、以及是否完成收集的决策。

与 Dispatcher 的区别：
  - Dispatcher 负责"去哪里"（路由决策）
  - Consult Agent 负责"问什么"（症状收集）
"""

from pydantic import BaseModel, Field

from app.agent.prompts import CONSULT_PROMPT
from app.graph.state import normalize_messages
from app.llm.client import LLMClient


class ConsultResult(BaseModel):
    """Consult Agent 的单轮输出结构。"""

    updated_slots: dict = Field(
        description="更新后的症状槽位，将新信息合并到已有槽位中"
    )
    response: str = Field(
        description="本轮回复文本。ask 时为追问语，done 时为过渡语"
    )
    next_action: str = Field(
        description="下一步动作：'ask'（继续追问）或 'done'（完成收集）"
    )
    summary: str = Field(
        default="",
        description="症状摘要。next_action='done' 时必填，用于给 Recommend 节点提供上下文"
    )


async def run_consult(
    llm_client: LLMClient,
    messages: list[dict],
    current_slots: dict,
    max_rounds: int = 6,
    consult_rounds: int = 0,
) -> ConsultResult:
    """执行一轮症状问诊。

    这个函数由 Consult 节点（app/graph/nodes/consult.py）调用。
    每轮：
      1. 使用 state 中显式追踪的 consult_rounds（不再从消息内容反推）
      2. 把当前 slots 和轮数作为上下文发给 LLM
      3. LLM 分析用户最新消息，更新 slots，决定下一步动作
      4. 如果已达最大轮数，强制 done

    Args:
        llm_client:     LLM 客户端
        messages:       完整对话历史（[{"role": "user"|"assistant", "content": "..."}]）
        current_slots:  当前已收集的症状槽位
        max_rounds:     最大追问轮数。超过此数不管是否充分都强制 done
        consult_rounds: 当前已追问轮数（由 consult_node 从 state 中传入并递增）

    Returns:
        ConsultResult：更新后的槽位、回复文本、下一步动作、症状摘要
    """
    # 标准化消息格式（LangGraph 可能把 dict 转成了 LangChain 对象）
    messages = normalize_messages(messages)

    # ── 如果已达最大追问轮数，强制结束 ──
    if consult_rounds >= max_rounds:
        symptoms_list = current_slots.get("symptoms", [])
        if symptoms_list:
            symptom_names = [
                s.get("name", "") for s in symptoms_list if isinstance(s, dict)
            ]
        else:
            symptom_names = ["不适"]
        summary = (
            f"用户症状：{'、'.join(symptom_names)}，"
            f"持续约{current_slots.get('duration_days', '未知')}天。"
        )
        return ConsultResult(
            updated_slots=current_slots,
            response="好的，根据您提供的信息，我已经基本了解了您的情况。让我为您推荐合适的药品。",
            next_action="done",
            summary=summary,
        )

    # ── 构建 LLM 调用消息 ──
    system_msg = {"role": "system", "content": CONSULT_PROMPT}

    context_msg = {
        "role": "system",
        "content": (
            f"## 当前已收集的症状信息 (slots)\n"
            f"```json\n{current_slots}\n```\n"
            f"## 已追问轮数: {consult_rounds}/{max_rounds}\n"
            + (f"⚠️ 已达到最大追问轮数，请务必判定为 done。"
               if consult_rounds >= max_rounds else "")
        ),
    }

    # 取最近 10 条消息作为上下文（太长会超过 token 限制）
    full_messages = [system_msg, context_msg] + messages[-10:]

    # ── 调用 LLM ──
    #todo
    result = await llm_client.generate_structured(
        messages=full_messages,
        schema=ConsultResult,
        temperature=0.3,
        max_tokens=1024,
    )

    # ── 合并槽位 ──
    # LLM 可能把已有槽位返回为 None（没改动的字段），需要保留旧值
    merged_slots = {**current_slots}
    for key, value in result.updated_slots.items():
        if value is not None or key not in merged_slots:
            merged_slots[key] = value

    return ConsultResult(
        updated_slots=merged_slots,
        response=result.response,
        next_action=result.next_action,
        summary=result.summary,
    )
