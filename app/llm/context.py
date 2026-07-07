"""
LLM 调用上下文 — 用 contextvars 跨调用栈传递 session_id / turn_id。

为什么用 ContextVar 而非显式传参？
  - LLMClient 的方法调用栈很深（Graph node → Agent → LLMClient）
  - 显式传参需要在 7 个调用点修改签名，容易遗漏
  - ContextVar 是 Python 标准库原生支持，协程安全（asyncio 自动传递）

使用方式：
    # chat.py — 在 Graph 执行前设置
    token_sess = set_llm_session(session_id)
    token_turn = set_llm_turn(turn_id)
    try:
        async for event in graph.astream_events(state, version="v2"):
            ...
    finally:
        reset_llm_session(token_sess)
        reset_llm_turn(token_turn)

    # LLMClient._schedule_log() — 自动补全
    if data.session_id is None:
        data.session_id = get_llm_session()
"""

import contextvars

_llm_session_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "llm_session_id", default=None
)
_llm_turn_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "llm_turn_id", default=None
)


def set_llm_session(session_id: str) -> contextvars.Token:
    """设置当前协程的 LLM 调用 session_id。返回 Token 用于 reset。"""
    return _llm_session_ctx.set(session_id)


def get_llm_session() -> str | None:
    """获取当前协程的 LLM 调用 session_id。"""
    return _llm_session_ctx.get()


def reset_llm_session(token: contextvars.Token) -> None:
    """恢复 LLM session_id 上下文。"""
    _llm_session_ctx.reset(token)


def set_llm_turn(turn_id: str) -> contextvars.Token:
    """设置当前协程的 LLM 调用 turn_id。返回 Token 用于 reset。"""
    return _llm_turn_ctx.set(turn_id)


def get_llm_turn() -> str | None:
    """获取当前协程的 LLM 调用 turn_id。"""
    return _llm_turn_ctx.get()


def reset_llm_turn(token: contextvars.Token) -> None:
    """恢复 LLM turn_id 上下文。"""
    _llm_turn_ctx.reset(token)
