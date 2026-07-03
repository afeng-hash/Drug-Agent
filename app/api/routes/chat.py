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
"""

import json
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.api.schemas import ChatRequest
from app.db.database import get_db
from app.db.repositories.session import SessionRepository
from app.graph.state import initial_state

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])


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

    # ═══════════════════════════════════════════════════════
    # 阶段 1：加载 session + 保存用户消息 + 加载历史
    # ═══════════════════════════════════════════════════════
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
        # 新的 session 首个 turn 时 state_snapshot 为 None → initial_state 使用默认空值
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
            .options(selectinload(SessionModel.messages))  # 一次性加载所有消息
            .where(SessionModel.session_id == session_id)
        )
        result = await db.execute(stmt)
        session_with_msgs = result.scalar_one_or_none()

        # 1.4 转为 [{"role": "...", "content": "..."}] 格式
        messages_history = [
            {"role": m.role, "content": m.content}
            for m in (session_with_msgs.messages if session_with_msgs else [])
        ]

    # ═══════════════════════════════════════════════════════
    # 阶段 2：初始化 state 并获取 graph
    # ═══════════════════════════════════════════════════════
    state = initial_state(
        session_id=session_id,
        messages=messages_history,
        snapshot=state_snapshot,
    )
    graph = app_state.graph

    # ═══════════════════════════════════════════════════════
    # 阶段 3：流式执行 Graph + 推送 SSE 事件
    # ═══════════════════════════════════════════════════════
    async def event_generator():
        """异步生成器：逐事件 yield SSE 字符串。"""
        start_time = time.time()
        total_tokens = 0

        try:
            # astream_events v2 提供最详细的事件流
            async for event in graph.astream_events(state, version="v2"):
                event_type = event.get("event", "")

                if event_type == "on_chain_start":
                    # ── 节点开始 ──
                    node_name = event.get("name", "")
                    if node_name in (
                        "intake", "dispatcher", "consult", "safety_check",
                        "recommend", "explain", "inventory", "end",
                    ):
                        yield _sse("node", {"node": node_name, "status": "started"})

                elif event_type == "on_chain_end":
                    # ── 节点结束，提取 state 更新 ──
                    node_name = event.get("name", "")
                    output = event.get("data", {}).get("output", {})

                    # Dispatcher 路由结果
                    if node_name == "dispatcher" and isinstance(output, dict):
                        route = output.get("dispatcher_result", {}).get("route", "")
                        yield _sse("node", {"node": "dispatcher", "route": route})

                    # 节点输出的 response → 分块模拟流式 token
                    if isinstance(output, dict) and output.get("response"):
                        response_text = output["response"]
                        chunk_size = 10  # 每块 10 字符
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
                    # ── LLM 流式 token（如使用 stream 模式时） ──
                    chunk = event.get("data", {}).get("chunk", {})
                    if hasattr(chunk, "choices") and chunk.choices:
                        delta = chunk.choices[0].delta
                        if hasattr(delta, "content") and delta.content:
                            yield _sse("token", {"content": delta.content})
                            total_tokens += 1

            # ── 完成 ──
            elapsed = round(time.time() - start_time, 2)
            yield _sse("done", {
                "session_id": session_id,
                "elapsed_seconds": elapsed,
                "usage": {"tokens": total_tokens},
            })

        except Exception as e:
            # Graph 执行异常 → 返回错误事件
            yield _sse("error", {
                "code": "INTERNAL_ERROR",
                "message": str(e),
            })

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 禁用 Nginx 缓冲
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
