"""
Admin 对话管理 — 查看/搜索/导出所有会话。

GET  /api/v1/admin/conversations          — 分页列表 + 筛选
GET  /api/v1/admin/conversations/{id}     — 完整对话详情
GET  /api/v1/admin/conversations/{id}/export — 导出
"""

import csv
import json
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.routes.admin.schemas import PaginatedResponse
from app.db.database import get_db
from app.db.models import Message, Session as SessionModel, User

router = APIRouter(prefix="/conversations", tags=["admin"])


# ── Schema ──────────────────────────────────────────────────

class ConversationListItem(BaseModel):
    session_id: str
    user_id: int | None
    user_nickname: str | None
    status: str
    message_count: int
    first_message: str
    last_message_at: str | None
    intents: list[str]
    recommendation_count: int
    created_at: str


class ConversationDetail(BaseModel):
    session_id: str
    user_id: int | None
    user_nickname: str | None
    status: str
    created_at: str
    expires_at: str | None
    updated_at: str | None
    messages: list[dict]
    state_snapshot: dict | None


# ── Helpers ──────────────────────────────────────────────────

def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts else None


# ── Routes ──────────────────────────────────────────────────

@router.get("")
async def list_conversations(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    status: str | None = Query(default=None, description="active|expired|closed"),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    user_id: int | None = Query(default=None),
    keyword: str | None = Query(default=None, description="搜索消息内容"),
) -> PaginatedResponse[ConversationListItem]:
    """分页查询会话列表。支持按状态、日期范围、用户、关键词筛选。"""
    async with get_db() as db:
        # ── 预聚合子查询（消除 N+1）──

        # 每个 session 的消息统计
        msg_stats = (
            select(
                Message.session_id,
                func.count(Message.id).label("msg_count"),
                func.max(Message.created_at).label("last_msg_at"),
            )
            .group_by(Message.session_id)
            .subquery()
        )

        # 每个 session 的 intents 聚合
        intent_agg = (
            select(
                Message.session_id,
                func.array_remove(
                    func.array_agg(func.distinct(Message.intent)), None
                ).label("intents"),
            )
            .where(Message.role == "user")
            .group_by(Message.session_id)
            .subquery()
        )

        # ── 主查询（一次 DB 往返）──
        base = (
            select(
                SessionModel.id,
                SessionModel.session_id,
                SessionModel.user_id,
                SessionModel.status,
                SessionModel.created_at,
                SessionModel.state_snapshot,
                User.nickname.label("user_nickname"),
                func.coalesce(msg_stats.c.msg_count, 0).label("message_count"),
                func.coalesce(msg_stats.c.last_msg_at, SessionModel.created_at).label(
                    "last_message_at"
                ),
                func.coalesce(intent_agg.c.intents, []).label("intents"),
            )
            .outerjoin(User, SessionModel.user_id == User.id)
            .outerjoin(
                msg_stats, SessionModel.id == msg_stats.c.session_id,
            )
            .outerjoin(
                intent_agg, SessionModel.id == intent_agg.c.session_id,
            )
        )

        # ── 筛选条件 ──
        if status:
            base = base.where(SessionModel.status == status)
        if user_id is not None:
            base = base.where(SessionModel.user_id == user_id)
        if date_from:
            base = base.where(SessionModel.created_at >= datetime.fromisoformat(date_from))
        if date_to:
            base = base.where(SessionModel.created_at <= datetime.fromisoformat(date_to))
        if keyword:
            base = base.where(
                SessionModel.id.in_(
                    select(Message.session_id).where(
                        Message.content.ilike(f"%{keyword}%")
                    )
                )
            )

        # ── 排序 + 总数 ──
        base = base.order_by(SessionModel.created_at.desc())

        count_q = select(func.count()).select_from(base.subquery())
        total = (await db.execute(count_q)).scalar() or 0

        # ── 分页 ──
        offset = (page - 1) * page_size
        rows = (await db.execute(base.offset(offset).limit(page_size))).all()

        # ── 补充每行的 first_message 和 recommendation_count ──
        # 这 2 个字段用标量子查询，每行 2 次 DB 往返（有索引，性能可接受）
        items = []
        for row in rows:
            d = row._asdict()
            sid = d["id"]  # sessions.id (int FK)

            # first_message: 标量子查询（仅此一个额外查询）
            first_msg = (
                await db.execute(
                    select(Message.content)
                    .where(Message.session_id == sid)
                    .where(Message.role == "user")
                    .order_by(Message.created_at.asc())
                    .limit(1)
                )
            ).scalar()

            # recommendation_count: 从 state_snapshot JSON 计算
            snap = d.get("state_snapshot")
            rec_count = 0
            if isinstance(snap, dict):
                recs = snap.get("recommendations", [])
                rec_count = len(recs) if isinstance(recs, list) else 0

            items.append(ConversationListItem(
                session_id=d["session_id"],
                user_id=d["user_id"],
                user_nickname=d["user_nickname"],
                status=d["status"],
                message_count=d["message_count"],
                first_message=(first_msg or "")[:100],
                last_message_at=_iso(d["last_message_at"]),
                intents=list(d["intents"] or []),
                recommendation_count=rec_count,
                created_at=_iso(d["created_at"]),
            ))

        return PaginatedResponse(
            items=items, total=total, page=page, page_size=page_size,
        )


@router.get("/{session_id}")
async def get_conversation(
    session_id: str,
    request: Request,
) -> ConversationDetail:
    """获取会话完整详情（消息列表 + 状态快照）。"""
    async with get_db() as db:
        from sqlalchemy.orm import selectinload

        stmt = (
            select(SessionModel)
            .options(selectinload(SessionModel.messages))
            .where(SessionModel.session_id == session_id)
        )
        result = await db.execute(stmt)
        session = result.scalar_one_or_none()

        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")

        user_nickname = None
        if session.user_id:
            user = await db.get(User, session.user_id)
            user_nickname = user.nickname if user else None

        messages = []
        for m in (session.messages or []):
            messages.append({
                "role": m.role,
                "content": m.content,
                "intent": m.intent,
                "metadata": m.metadata_,
                "timestamp": _iso(m.created_at),
            })

        return ConversationDetail(
            session_id=session.session_id,
            user_id=session.user_id,
            user_nickname=user_nickname,
            status=session.status,
            created_at=_iso(session.created_at),
            expires_at=_iso(session.expires_at),
            updated_at=_iso(session.updated_at),
            messages=messages,
            state_snapshot=session.state_snapshot,
        )


@router.get("/{session_id}/export")
async def export_conversation(
    session_id: str,
    format: str = Query(default="json", description="json|csv"),
):
    """导出会话数据（流式传输，避免大 session OOM）。"""
    async with get_db() as db:
        from sqlalchemy.orm import selectinload

        stmt = (
            select(SessionModel)
            .options(selectinload(SessionModel.messages))
            .where(SessionModel.session_id == session_id)
        )
        result = await db.execute(stmt)
        session = result.scalar_one_or_none()

        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")

        if format == "csv":
            filename = f"conversation_{session_id}.csv"

            async def csv_generator():
                """逐行 yield CSV，避免一次性加载全部内容到内存。"""
                yield "role,content,intent,timestamp\n"
                for m in (session.messages or []):
                    # CSV 转义：字段含逗号或引号时用引号包裹
                    content_escaped = m.content.replace('"', '""')
                    row = f'{m.role},"{content_escaped}",{m.intent or ""},{_iso(m.created_at) or ""}\n'
                    yield row

            return StreamingResponse(
                csv_generator(),
                media_type="text/csv",
                headers={"Content-Disposition": f"attachment; filename={filename}"},
            )
        else:
            filename = f"conversation_{session_id}.json"

            async def json_generator():
                """逐块 yield JSON，避免一次性序列化所有消息。"""
                yield '{\n'
                yield f'  "session_id": "{session_id}",\n'
                yield f'  "status": "{session.status}",\n'
                yield '  "messages": [\n'
                msgs = session.messages or []
                for i, m in enumerate(msgs):
                    comma = "," if i < len(msgs) - 1 else ""
                    msg_json = json.dumps({
                        "role": m.role,
                        "content": m.content,
                        "intent": m.intent,
                        "timestamp": _iso(m.created_at),
                    }, ensure_ascii=False)
                    yield f"    {msg_json}{comma}\n"
                yield '  ],\n'
                snapshot_json = json.dumps(
                    session.state_snapshot, ensure_ascii=False, indent=2
                ) if session.state_snapshot else "null"
                # indent state_snapshot
                yield f'  "state_snapshot": {snapshot_json}\n'
                yield '}\n'

            return StreamingResponse(
                json_generator(),
                media_type="application/json",
                headers={"Content-Disposition": f"attachment; filename={filename}"},
            )
