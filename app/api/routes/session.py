"""
Session management endpoints — 会话的创建和查询。

POST /api/v1/sessions        — 创建新会话
GET  /api/v1/sessions/{id}   — 查询会话详情（含消息历史）

会话是匿名的：不需要用户登录，用 UUID 标识。
默认 30 分钟无活动自动过期。
"""

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.schemas import SessionDetailResponse, SessionResponse
from app.db.database import get_db
from app.db.models import Session as SessionModel
from app.db.repositories.session import SessionRepository

router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])


@router.post("", response_model=SessionResponse, status_code=201)
async def create_session(request: Request) -> SessionResponse:
    """创建一个新的匿名会话。

    前端在每次新对话开始时调用此接口获取 session_id。
    session_id 用于后续所有 POST /api/v1/chat/{session_id} 请求。

    不需要请求体。

    Returns:
        201: SessionResponse（session_id, status, created_at, expires_at）
    """
    settings = request.app.state.settings

    async with get_db() as db:
        repo = SessionRepository(db, expire_minutes=settings.session_expire_minutes)
        session = await repo.create()
        return SessionResponse(
            session_id=session.session_id,
            status=session.status,
            created_at=session.created_at.isoformat(),
            expires_at=session.expires_at.isoformat(),
        )


@router.get("/{session_id}", response_model=SessionDetailResponse)
async def get_session(
    session_id: str,
    request: Request,
) -> SessionDetailResponse:
    """查询会话状态和历史消息。

    可用于：
      - 恢复对话（用户刷新页面后重建聊天记录）
      - 检查会话是否过期
      - 查看之前的推荐结果

    Args:
        session_id: 会话 UUID（路径参数）

    Returns:
        200: SessionDetailResponse（含消息列表）

    Raises:
        404: 会话不存在
    """
    settings = request.app.state.settings

    async with get_db() as db:
        repo = SessionRepository(db, expire_minutes=settings.session_expire_minutes)

        # 查询并自动处理过期
        session = await repo.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")

        # 加载消息历史（eager load，避免 N+1）
        stmt = (
            select(SessionModel)
            .options(selectinload(SessionModel.messages))
            .where(SessionModel.session_id == session_id)
        )
        result = await db.execute(stmt)
        session_with_msgs = result.scalar_one_or_none()

        messages = []
        if session_with_msgs and session_with_msgs.messages:
            messages = [
                {
                    "role": m.role,
                    "content": m.content,
                    "timestamp": m.created_at.isoformat(),
                }
                for m in session_with_msgs.messages
            ]

        return SessionDetailResponse(
            session_id=session.session_id,
            status=session.status,
            created_at=session.created_at.isoformat(),
            expires_at=session.expires_at.isoformat(),
            messages=messages,
        )
