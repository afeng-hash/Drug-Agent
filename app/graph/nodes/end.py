"""
End node — 轮次收尾与持久化。

每个 Graph turn 的最后一个节点。负责：
  1. 将 AI 回复作为 assistant 消息写入 session 消息历史
  2. 如果有安全筛查结果，记录到 safety_logs 表（审计用）
  3. 标记 phase="ended"
"""

from app.db.repositories.safety_log import SafetyLogRepository
from app.db.repositories.session import SessionRepository


async def end_node(
    state: dict,
    session_repo: SessionRepository,
    safety_log_repo: SafetyLogRepository | None = None,
) -> dict:
    """持久化本轮对话结果。

    Args:
        state:            当前会话状态
        session_repo:     会话仓库（已绑定 DB session）
        safety_log_repo:  安全日志仓库（可能为 None，如 explain 分支）

    Returns:
        state 更新：phase="ended"，node_events
    """
    session_id = state.get("session_id", "")
    response = state.get("response", "")
    dispatcher_result = state.get("dispatcher_result", {})
    intent = dispatcher_result.get("intent")

    # ── 1. 保存 AI 回复到消息历史 ──
    if session_id and response:
        try:
            await session_repo.add_message(
                session_id=session_id,
                role="assistant",
                content=response,
                intent=intent,
                metadata={"phase": state.get("phase")},
            )
        except Exception:
            pass  # 持久化失败不影响用户体验（消息已经在 SSE 中推送了）

    # ── 2. 记录安全筛查结果 ──
    safety_result = state.get("safety_result")
    if safety_result and safety_log_repo and session_id:
        try:
            # 先查出内部 session.id（int），这是 safety_logs 表的外键
            session_obj = await session_repo.get(session_id)
            if session_obj:
                await safety_log_repo.log(
                    session_id=session_obj.id,
                    verdict=safety_result.get("verdict", "PASS"),
                    triggered_rules=safety_result.get("triggered_rules", []),
                    input_slots=state.get("consult_slots", {}),
                )
        except Exception:
            pass  # 安全日志写入失败不阻塞主流程

    return {
        "phase": "ended",
        "node_events": [{"node": "end", "status": "ok"}],
    }
