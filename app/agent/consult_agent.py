"""ReAct-style consult agent — dynamic symptom gathering via LLM."""

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from app.agent.prompts import CONSULT_PROMPT
from app.graph.state import normalize_messages
from app.llm.client import LLMClient


class ConsultResult(BaseModel):
    """Structured output from the consult agent."""
    updated_slots: dict = Field(description="Updated slot values with new info merged")
    response: str = Field(description="Natural language response to the user")
    next_action: str = Field(description="'ask' to continue; 'done' if slots are sufficient")
    summary: str = Field(default="", description="Symptom summary when done")


async def run_consult(
    llm_client: LLMClient,
    messages: list[dict],
    current_slots: dict,
    max_rounds: int = 6,
) -> ConsultResult:
    """Run one round of consult: analyze + update slots + decide next action.

    Args:
        llm_client: The LLM client instance.
        messages: Full conversation history (list of {role, content}).
        current_slots: Current ConsultSlots state.
        max_rounds: Maximum consult rounds before forcing done.

    Returns:
        ConsultResult with updated slots, response, next_action, summary.
    """
    # Normalize messages: LangGraph may convert dicts to LangChain objects
    messages = normalize_messages(messages)

    # Count how many times we've asked (assistant messages during consult)
    consult_rounds = sum(
        1 for m in messages
        if m.get("role") == "assistant" and "问" not in m.get("content", "")
    )

    # Build system message with current state
    system_msg = {
        "role": "system",
        "content": CONSULT_PROMPT,
    }

    # Build context with current slots
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

    # Force done if max rounds reached — bypass LLM call
    if consult_rounds >= max_rounds:
        symptoms_list = current_slots.get("symptoms", [])
        if symptoms_list:
            symptom_names = [
                s.get("name", "") for s in symptoms_list if isinstance(s, dict)
            ]
        else:
            symptom_names = ["不适"]
        summary = f"用户症状：{'、'.join(symptom_names)}，持续约{current_slots.get('duration_days', '未知')}天。"  # noqa: E501
        return ConsultResult(
            updated_slots=current_slots,
            response="好的，根据您提供的信息，我已经基本了解了您的情况。让我为您推荐合适的药品。",
            next_action="done",
            summary=summary,
        )

    # Call LLM
    full_messages = [system_msg, context_msg] + messages[-10:]  # Last 10 msgs for context
    result = await llm_client.generate_structured(
        messages=full_messages,
        schema=ConsultResult,
        temperature=0.3,
        max_tokens=1024,
    )

    # Merge slots: don't lose existing values that LLM returned as None
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
