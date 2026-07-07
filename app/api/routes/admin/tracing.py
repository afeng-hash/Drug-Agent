"""
Admin Agent Trace — 会话级链路追踪 + Turn 级详情。

GET  /api/v1/admin/traces              — Trace 会话列表
GET  /api/v1/admin/traces/{turn_id}    — 单 turn 完整链路
GET  /api/v1/admin/traces/stats        — 聚合统计
"""

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.routes.admin.schemas import PaginatedResponse
from app.db.database import get_db
from app.db.models import LLMCallLog, Message, Session as SessionModel, TraceLog

router = APIRouter(prefix="/traces", tags=["admin"])


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts else None


def _strip_uuid_suffix(turn_id: str) -> str:
    """剥离 turn_id 末尾的 uuid8 后缀（新格式 → 旧格式兼容）。

    新格式: "{session_id}:{count}:{uuid8}"  → 剥离后: "{session_id}:{count}"
    旧格式: "{session_id}:{count}"          → 不变

    用于向后兼容查询：当精确匹配新格式 turn_id 找不到历史数据时，
    用剥离后的前缀做 LIKE 查询。
    """
    parts = turn_id.rsplit(":", 1)
    if len(parts) == 3 and len(parts[-1]) == 8:
        # 最后一段是 8 位 hex → 新格式，剥离
        return f"{parts[0]}:{parts[1]}"
    return turn_id  # 旧格式或无法识别，保持原样


# ── Schema ──────────────────────────────────────────────────


class TraceSessionItem(BaseModel):
    session_id: str
    turn_count: int
    total_duration_ms: float | None
    error_count: int
    first_node: str | None
    last_node: str | None
    created_at: str | None


class NodeTraceItem(BaseModel):
    node: str
    status: str
    duration_ms: float | None
    metadata: dict | None
    error_message: str | None
    started_at: str | None


class TurnTraceDetail(BaseModel):
    turn_id: str
    session_id: str
    nodes: list[NodeTraceItem]
    llm_calls: list[dict]
    messages: list[dict]


class TraceStats(BaseModel):
    total_turns: int
    error_rate: float
    avg_duration_ms: float | None


# ── Routes ──────────────────────────────────────────────────


@router.get("")
async def list_traces(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    status: str | None = Query(default=None, description="completed|error"),
    keyword: str | None = Query(default=None),
) -> PaginatedResponse[TraceSessionItem]:
    """分页查询 Trace 会话列表。

    每个 item 聚合了一个 session 的所有 turn 的 trace 概览。
    """
    async with get_db() as db:
        # 按 session_id 聚合
        agg = (
            select(
                TraceLog.session_id,
                func.count(func.distinct(TraceLog.turn_id)).label("turn_count"),
                func.coalesce(func.sum(TraceLog.duration_ms), 0.0).label("total_duration"),
                func.count(TraceLog.id).filter(TraceLog.status == "error").label("error_count"),
                func.min(TraceLog.started_at).label("first_at"),
            )
            .group_by(TraceLog.session_id)
            .subquery()
        )

        base = select(
            agg.c.session_id,
            agg.c.turn_count,
            agg.c.total_duration,
            agg.c.error_count,
            agg.c.first_at,
        )

        # 筛选
        if date_from:
            base = base.where(agg.c.first_at >= datetime.fromisoformat(date_from))
        if date_to:
            base = base.where(agg.c.first_at <= datetime.fromisoformat(date_to))

        if keyword:
            base = base.where(
                agg.c.session_id.in_(
                    select(Message.session_id).where(
                        Message.content.ilike(f"%{keyword}%")
                    )
                )
            )

        count_q = select(func.count()).select_from(base.subquery())
        total = (await db.execute(count_q)).scalar() or 0

        offset = (page - 1) * page_size
        base = base.order_by(agg.c.first_at.desc())
        rows = (await db.execute(base.offset(offset).limit(page_size))).all()

        items = []
        for row in rows:
            d = row._asdict()
            session_id = d["session_id"]
            total_dur = float(d["total_duration"]) if d["total_duration"] else None

            # 首尾节点
            first_last = (
                await db.execute(
                    select(
                        TraceLog.node,
                    )
                    .where(TraceLog.session_id == session_id)
                    .order_by(TraceLog.started_at.asc())
                    .limit(1)
                )
            ).first()
            first_node = first_last[0] if first_last else None

            last_node_row = (
                await db.execute(
                    select(TraceLog.node)
                    .where(TraceLog.session_id == session_id)
                    .order_by(TraceLog.started_at.desc())
                    .limit(1)
                )
            ).first()
            last_node = last_node_row[0] if last_node_row else None

            items.append(
                TraceSessionItem(
                    session_id=session_id,
                    turn_count=d["turn_count"],
                    total_duration_ms=round(total_dur, 1) if total_dur else None,
                    error_count=d["error_count"],
                    first_node=first_node,
                    last_node=last_node,
                    created_at=_iso(d["first_at"]),
                )
            )

        return PaginatedResponse(
            items=items, total=total, page=page, page_size=page_size,
        )


@router.get("/{turn_id}")
async def get_turn_trace(turn_id: str, request: Request) -> TurnTraceDetail:
    """获取单个 turn 的完整链路。

    包含：节点执行时间线 + LLM 调用详情 + 关联消息。
    """
    async with get_db() as db:
        # 节点列表 — 同时匹配新旧格式（向后兼容）
        base_turn = _strip_uuid_suffix(turn_id)
        turn_filters = [TraceLog.turn_id == turn_id]
        if base_turn != turn_id:
            turn_filters.append(TraceLog.turn_id == base_turn)

        from sqlalchemy import or_

        node_rows = (
            await db.execute(
                select(TraceLog)
                .where(or_(*turn_filters))
                .order_by(TraceLog.started_at.asc())
            )
        ).scalars().all()

        if not node_rows:
            raise HTTPException(status_code=404, detail="Turn not found")

        session_id = node_rows[0].session_id

        nodes = [
            NodeTraceItem(
                node=n.node,
                status=n.status,
                duration_ms=round(n.duration_ms, 1) if n.duration_ms else None,
                metadata=n.metadata_,
                error_message=n.error_message,
                started_at=_iso(n.started_at),
            )
            for n in node_rows
        ]

        # 关联 LLM 调用 — 同时匹配新旧格式
        llm_filters = [LLMCallLog.turn_id == turn_id]
        if base_turn != turn_id:
            llm_filters.append(LLMCallLog.turn_id == base_turn)

        llm_rows = (
            await db.execute(
                select(LLMCallLog)
                .where(or_(*llm_filters))
                .order_by(LLMCallLog.created_at.asc())
            )
        ).scalars().all()

        # 最后兜底: 如果 turn 级查询不到，降级到 session 级查询
        if not llm_rows:
            llm_rows = (
                await db.execute(
                    select(LLMCallLog)
                    .where(LLMCallLog.session_id == session_id)
                    .order_by(LLMCallLog.created_at.asc())
                )
            ).scalars().all()

        llm_calls = [
            {
                "id": c.id,
                "node": c.node,
                "model": c.model,
                "prompt_tokens": c.prompt_tokens,
                "completion_tokens": c.completion_tokens,
                "latency_ms": c.latency_ms,
                "success": c.success,
                "error_message": c.error_message,
                "created_at": _iso(c.created_at),
            }
            for c in llm_rows
        ]

        # 关联消息（通过 session_id 查 messages 表）
        from sqlalchemy import and_

        # 获取该 session 的内部 id
        sess_row = (
            await db.execute(
                select(SessionModel.id).where(SessionModel.session_id == session_id)
            )
        ).scalar_one_or_none()

        msg_rows = []
        if sess_row:
            msg_rows = (
                await db.execute(
                    select(Message)
                    .where(Message.session_id == sess_row)
                    .order_by(Message.created_at.asc())
                )
            ).scalars().all()

        messages = [
            {
                "role": m.role,
                "content": m.content[:500],  # 截断长内容
                "intent": m.intent,
                "timestamp": _iso(m.created_at),
            }
            for m in msg_rows
        ]

        return TurnTraceDetail(
            turn_id=turn_id,
            session_id=session_id,
            nodes=nodes,
            llm_calls=llm_calls,
            messages=messages,
        )


@router.get("/stats")
async def trace_stats(
    request: Request,
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
) -> TraceStats:
    """链路追踪聚合统计。"""
    async with get_db() as db:
        base = select(
            func.count(func.distinct(TraceLog.turn_id)),
            func.count(TraceLog.id).filter(TraceLog.status == "error"),
            func.avg(TraceLog.duration_ms),
        )

        if date_from:
            base = base.where(TraceLog.started_at >= datetime.fromisoformat(date_from))
        if date_to:
            base = base.where(TraceLog.started_at <= datetime.fromisoformat(date_to))

        row = (await db.execute(base)).one()

        total = row[0] or 0
        errors = row[1] or 0

        return TraceStats(
            total_turns=total,
            error_rate=round(errors / total, 4) if total > 0 else 0.0,
            avg_duration_ms=round(row[2], 1) if row[2] else None,
        )
