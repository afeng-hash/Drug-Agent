"""
Admin 用户管理 — 查看用户及其会话历史。

GET  /api/v1/admin/users           — 分页列表 + 搜索
GET  /api/v1/admin/users/{id}      — 用户详情
GET  /api/v1/admin/users/{id}/sessions — 用户的全部会话
"""

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.routes.admin.schemas import PaginatedResponse
from app.db.database import get_db
from app.db.models import Message, Session as SessionModel, User

router = APIRouter(prefix="/users", tags=["admin"])


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts else None


# ── Schema ──────────────────────────────────────────────────


class UserListItem(BaseModel):
    id: int
    external_id: str
    nickname: str | None
    session_count: int
    last_active_at: str | None
    created_at: str | None


class UserDetail(BaseModel):
    id: int
    external_id: str
    nickname: str | None
    health_profile: dict
    session_count: int
    last_active_at: str | None
    created_at: str | None
    recent_sessions: list[dict]


# ── Routes ──────────────────────────────────────────────────


@router.get("")
async def list_users(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    search: str | None = Query(default=None, description="搜索 external_id 或 nickname"),
) -> PaginatedResponse[UserListItem]:
    """分页查询用户列表。"""
    async with get_db() as db:
        base = select(
            User.id,
            User.external_id,
            User.nickname,
            User.last_active_at,
            User.created_at,
        )

        if search:
            base = base.where(
                User.external_id.ilike(f"%{search}%")
                | User.nickname.ilike(f"%{search}%")
            )

        # 总数
        count_q = select(func.count()).select_from(base.subquery())
        total = (await db.execute(count_q)).scalar() or 0

        # 分页
        offset = (page - 1) * page_size
        base = base.order_by(User.last_active_at.desc())
        rows = (await db.execute(base.offset(offset).limit(page_size))).all()

        items = []
        for row in rows:
            d = row._asdict()
            uid = d["id"]

            # 会话数
            sess_count = (
                await db.execute(
                    select(func.count(SessionModel.id)).where(
                        SessionModel.user_id == uid
                    )
                )
            ).scalar() or 0

            items.append(
                UserListItem(
                    id=uid,
                    external_id=d["external_id"],
                    nickname=d["nickname"],
                    session_count=sess_count,
                    last_active_at=_iso(d["last_active_at"]),
                    created_at=_iso(d["created_at"]),
                )
            )

        return PaginatedResponse(
            items=items, total=total, page=page, page_size=page_size,
        )


@router.get("/{user_id}")
async def get_user(user_id: int, request: Request) -> UserDetail:
    """获取用户详情（含健康画像和最近会话）。"""
    async with get_db() as db:
        user = await db.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")

        # 会话数
        sess_count = (
            await db.execute(
                select(func.count(SessionModel.id)).where(
                    SessionModel.user_id == user_id
                )
            )
        ).scalar() or 0

        # 最近 10 条会话
        recent = (
            await db.execute(
                select(
                    SessionModel.session_id,
                    SessionModel.status,
                    SessionModel.created_at,
                )
                .where(SessionModel.user_id == user_id)
                .order_by(SessionModel.created_at.desc())
                .limit(10)
            )
        ).all()

        return UserDetail(
            id=user.id,
            external_id=user.external_id,
            nickname=user.nickname,
            health_profile=user.health_profile or {},
            session_count=sess_count,
            last_active_at=_iso(user.last_active_at),
            created_at=_iso(user.created_at),
            recent_sessions=[
                {"session_id": r.session_id, "status": r.status, "created_at": _iso(r.created_at)}
                for r in recent
            ],
        )


@router.get("/{user_id}/sessions")
async def get_user_sessions(
    user_id: int,
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """获取用户的全部会话列表。"""
    async with get_db() as db:
        user = await db.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")

        # 会话列表 + 消息数
        base = (
            select(
                SessionModel.session_id,
                SessionModel.status,
                SessionModel.created_at,
                func.count(Message.id).label("message_count"),
            )
            .outerjoin(Message, SessionModel.id == Message.session_id)
            .where(SessionModel.user_id == user_id)
            .group_by(SessionModel.id)
            .order_by(SessionModel.created_at.desc())
        )

        count_q = select(func.count()).select_from(base.subquery())
        total = (await db.execute(count_q)).scalar() or 0

        offset = (page - 1) * page_size
        rows = (await db.execute(base.offset(offset).limit(page_size))).all()

        items = [
            {
                "session_id": r.session_id,
                "status": r.status,
                "created_at": _iso(r.created_at),
                "message_count": r.message_count,
            }
            for r in rows
        ]

        return PaginatedResponse(
            items=items, total=total, page=page, page_size=page_size,
        )
