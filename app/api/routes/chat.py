"""
Chat endpoint — SSE streaming of Graph execution events.

POST /api/v1/chat/{session_id}

这是系统最复杂的端点：接收用户消息，触发 LangGraph 状态机执行，
将执行过程中的各类事件通过 SSE (Server-Sent Events) 实时推送给前端。

为什么用 SSE 而非 WebSocket？
  - SSE 是单向流（服务端 → 客户端），语义匹配（一次请求一次完整执行）
  - 比 WebSocket 更轻量，不需要心跳维护
  - 浏览器原生 EventSource 支持（但 POST 场景需用 fetch + ReadableStream）

事件类型：node / token / safety / data / done / error
详见 API_DOCS.md

Trace 日志采集：
  - 在 astream_events 循环中收集每个节点的开始/结束事件
  - 流结束后 fire-and-forget 写入 trace_logs 表，不阻塞 SSE 响应
"""

import asyncio
import json
import time as time_module
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.api.schemas import ChatRequest
from app.db.database import get_db
from app.db.repositories.session import SessionRepository
from app.graph.state import initial_state

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])

# ── 需要追踪的 Graph 节点 ──
_TRACE_NODES = frozenset({
    "intake", "dispatcher", "consult", "safety_block",
    "recommend", "explain", "inventory", "react", "end",
})


def _extract_trace_meta(node_name: str, output: dict) -> dict | None:
    """从节点输出中提取 trace metadata。"""
    if node_name == "dispatcher" and isinstance(output, dict):
        dr = output.get("dispatcher_result", {})
        return {
            "route": dr.get("route"),
            "intent": dr.get("intent"),
            "actions": dr.get("actions"),
        }
    if node_name == "consult" and isinstance(output, dict):
        return {
            "next_action": output.get("next_action"),
            "rounds": output.get("consult_rounds"),
        }
    if node_name == "safety_block" and isinstance(output, dict):
        sr = output.get("safety_result", {})
        return {
            "verdict": sr.get("verdict"),
            "triggered_rules": sr.get("triggered_rules"),
        }
    if node_name == "recommend" and isinstance(output, dict):
        recs = output.get("recommendations", [])
        return {"count": len(recs)}
    if node_name == "end" and isinstance(output, dict):
        events = output.get("node_events", [])
        return {"events": events}
    return None


async def _write_trace_logs(
    turn_id: str, session_id: str, events: list[dict],
) -> None:
    """Fire-and-forget: 将 trace 事件批量写入 trace_logs 表。"""
    if not events:
        return
    try:
        from app.db.models import TraceLog

        async with get_db() as db:
            for evt in events:
                db.add(TraceLog(
                    session_id=session_id,
                    turn_id=turn_id,
                    node=evt["node"],
                    status=evt.get("status", "completed"),
                    duration_ms=evt.get("duration_ms"),
                    metadata_=evt.get("metadata"),
                    started_at=evt.get("started_at", datetime.now(timezone.utc)),
                    completed_at=evt.get("completed_at"),
                ))
            await db.commit()
    except Exception:
        pass  # 追踪日志写入失败不影响主流程


@router.post("/{session_id}")
async def chat(
    session_id: str,
    body: ChatRequest,
    request: Request,
) -> StreamingResponse:
    """处理用户消息，流式返回 AI 回复。

    请求流程：
      1. 校验 session 是否存在且为 active
      2. 将用户消息保存到 messages 表
      3. 加载该 session 的完整历史消息
      4. 用历史消息初始化 ConversationState
      5. 运行 LangGraph.astream_events() 流式执行
      6. 将 Graph 事件转换为 SSE 格式推送给前端

    Args:
        session_id: 会话 UUID（路径参数）
        body:       请求体 {"message": "用户输入"}
        request:    FastAPI Request（用于获取 app.state）

    Returns:
        StreamingResponse（text/event-stream）
    """
    app_state = request.app.state
    settings = app_state.settings

    # ── 阶段 1：加载 session + 保存用户消息 + 加载历史 ──
    # 在独立的 DB session 中完成，然后关闭（不让 session 跨 await 边界）
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from app.db.models import Session as SessionModel

    async with get_db() as db:
        session_repo = SessionRepository(db, expire_minutes=settings.session_expire_minutes)

        # 1.1 校验 session
        session = await session_repo.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        if session.status != "active":
            raise HTTPException(status_code=400, detail=f"Session is {session.status}")

        # 1.1b 提取上一 turn 的结构化状态快照（如有）
        state_snapshot = session.state_snapshot if session.state_snapshot else None

        # 1.2 保存用户消息到数据库
        await session_repo.add_message(
            session_id=session_id,
            role="user",
            content=body.message,
        )

        # 1.3 加载完整消息历史（eager load messages 关系）
        stmt = (
            select(SessionModel)
            .options(selectinload(SessionModel.messages))
            .where(SessionModel.session_id == session_id)
        )
        result = await db.execute(stmt)
        session_with_msgs = result.scalar_one_or_none()

        # 1.4 转为 [{"role": "...", "content": "..."}] 格式
        messages_history = [
            {"role": m.role, "content": m.content}
            for m in (session_with_msgs.messages if session_with_msgs else [])
        ]

    # ── 计算 turn_id（用于 trace 日志） ──
    # 使用 user_message_count + short uuid 防止并发碰撞
    # edge case: 若 add_message() 成功但后续逻辑失败，turn_id 已生成但不会写入 trace
    import uuid as _uuid
    user_message_count = sum(1 for m in messages_history if m["role"] == "user")
    turn_id = f"{session_id}:{user_message_count}:{_uuid.uuid4().hex[:8]}"

    # ── 阶段 2：初始化 state 并获取 graph ──
    state = initial_state(
        session_id=session_id,
        messages=messages_history,
        snapshot=state_snapshot,
    )
    graph = app_state.graph

    # ── 阶段 3：流式执行 Graph + 推送 SSE 事件 ──
    async def event_generator():
        """异步生成器：逐事件 yield SSE 字符串。"""
        start_time = time_module.time()
        total_tokens = 0
        # Trace 采集
        node_start_times: dict[str, float] = {}
        node_start_dt: dict[str, datetime] = {}  # 真正的开始时间戳
        trace_events: list[dict] = []

        # ── 设置 LLM 调用上下文（ContextVar）──
        from app.llm.context import set_llm_session, set_llm_turn
        from app.llm.context import reset_llm_session, reset_llm_turn
        token_sess = set_llm_session(session_id)
        token_turn = set_llm_turn(turn_id)

        try:
            async for event in graph.astream_events(state, version="v2"):
                event_type = event.get("event", "")

                if event_type == "on_chain_start":
                    node_name = event.get("name", "")
                    if node_name in _TRACE_NODES:
                        node_start_times[node_name] = time_module.time()
                        node_start_dt[node_name] = datetime.now(timezone.utc)
                    if node_name in (
                        "intake", "dispatcher", "consult", "safety_block",
                        "recommend", "explain", "inventory", "end",
                    ):
                        yield _sse("node", {"node": node_name, "status": "started"})

                elif event_type == "on_chain_end":
                    node_name = event.get("name", "")
                    output = event.get("data", {}).get("output", {})

                    # Trace 采集
                    if node_name in _TRACE_NODES:
                        t_start = node_start_times.get(node_name, time_module.time())
                        duration_ms = (time_module.time() - t_start) * 1000
                        completed_at = datetime.now(timezone.utc)
                        started_at = node_start_dt.get(node_name, completed_at)
                        trace_events.append({
                            "node": node_name,
                            "status": "completed",
                            "duration_ms": round(duration_ms, 1),
                            "metadata": _extract_trace_meta(node_name, output),
                            "started_at": started_at,
                            "completed_at": completed_at,
                        })

                    # Dispatcher 路由结果
                    if node_name == "dispatcher" and isinstance(output, dict):
                        route = output.get("dispatcher_result", {}).get("route", "")
                        yield _sse("node", {"node": "dispatcher", "route": route})

                    # 节点输出的 response → 分块模拟流式 token
                    if isinstance(output, dict) and output.get("response"):
                        response_text = output["response"]
                        chunk_size = 10
                        for i in range(0, len(response_text), chunk_size):
                            chunk = response_text[i:i + chunk_size]
                            yield _sse("token", {"content": chunk})
                            total_tokens += 1

                    # 安全筛查结果
                    safety = (output if isinstance(output, dict) else {}).get("safety_result")
                    if safety:
                        yield _sse("safety", {
                            "verdict": safety.get("verdict"),
                            "triggered_rules": safety.get("triggered_rules"),
                        })

                    # 推荐结果
                    recs = (output if isinstance(output, dict) else {}).get("recommendations")
                    if recs:
                        yield _sse("data", {
                            "phase": "recommending",
                            "recommendations": recs,
                        })

                elif event_type == "on_chat_model_stream":
                    chunk = event.get("data", {}).get("chunk", {})
                    if hasattr(chunk, "choices") and chunk.choices:
                        delta = chunk.choices[0].delta
                        if hasattr(delta, "content") and delta.content:
                            yield _sse("token", {"content": delta.content})
                            total_tokens += 1

            # ── 完成 ──
            elapsed = round(time_module.time() - start_time, 2)
            yield _sse("done", {
                "session_id": session_id,
                "elapsed_seconds": elapsed,
                "usage": {"tokens": total_tokens},
            })

        except Exception as e:
            # Graph 执行异常 → 为未完成的节点记录 error trace
            now = datetime.now(timezone.utc)
            for node_name, t_start in node_start_times.items():
                if not any(evt["node"] == node_name for evt in trace_events):
                    started_at = node_start_dt.get(node_name, now)
                    trace_events.append({
                        "node": node_name,
                        "status": "error",
                        "duration_ms": round((time_module.time() - t_start) * 1000, 1),
                        "metadata": {"error": str(e)},
                        "started_at": started_at,
                        "completed_at": now,
                    })
            yield _sse("error", {
                "code": "INTERNAL_ERROR",
                "message": str(e),
            })

        finally:
            # Fire-and-forget: 写入 trace_logs
            if trace_events:
                asyncio.create_task(_write_trace_logs(turn_id, session_id, trace_events))
            # 重置 LLM 调用上下文
            reset_llm_session(token_sess)
            reset_llm_turn(token_turn)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _sse(event: str, data: dict) -> str:
    """格式化一条 Server-Sent Event。

    SSE 格式：
      event: <事件名>\n
      data: <JSON>\n
      \n

    Args:
        event: 事件类型（node / token / safety / data / done / error）
        data:  事件数据（dict，自动序列化为 JSON）

    Returns:
        格式化的 SSE 字符串
    """
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
