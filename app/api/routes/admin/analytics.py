"""
Admin 用户访问分析 — 仪表盘数据。

GET  /api/v1/admin/analytics/overview    — 概览统计
GET  /api/v1/admin/analytics/trends      — 按天趋势
GET  /api/v1/admin/analytics/intents     — Intent 分布
GET  /api/v1/admin/analytics/conversion  — 转化漏斗
GET  /api/v1/admin/analytics/top-drugs   — Top 推荐药品
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.sql import case

from app.db.database import get_db
from app.db.models import Message, SafetyLog, Session as SessionModel

router = APIRouter(prefix="/analytics", tags=["admin"])


# ── Schema ──────────────────────────────────────────────────


class OverviewOut(BaseModel):
    total_sessions: int = 0
    active_sessions: int = 0
    total_messages: int = 0
    avg_messages_per_session: float = 0.0
    safety_block_rate: float = 0.0


class TrendItem(BaseModel):
    date: str
    sessions: int
    messages: int
    recommendations: int


class IntentItem(BaseModel):
    intent: str
    count: int


class ConversionFunnel(BaseModel):
    total_sessions: int
    with_symptoms: int           # 有症状描述
    recommendations_given: int  # 有推荐结果
    with_ai_response: int       # 有 AI 回复（含 assistant 消息）


class TopDrugItem(BaseModel):
    drug_name: str
    count: int


# ── Routes ──────────────────────────────────────────────────


@router.get("/overview")
async def analytics_overview(
    request: Request,
    days: int = Query(default=30, ge=1, le=365),
) -> OverviewOut:
    """整体概览统计。"""
    async with get_db() as db:
        since = datetime.now(timezone.utc) - timedelta(days=days)

        # 总会话数
        total_sess = (
            await db.execute(
                select(func.count(SessionModel.id))
                .where(SessionModel.created_at >= since)
            )
        ).scalar() or 0

        # 活跃会话
        active_sess = (
            await db.execute(
                select(func.count(SessionModel.id))
                .where(SessionModel.status == "active")
                .where(SessionModel.created_at >= since)
            )
        ).scalar() or 0

        # 总消息数
        total_msgs = (
            await db.execute(
                select(func.count(Message.id))
                .where(Message.created_at >= since)
            )
        ).scalar() or 0

        # 平均每会话消息数
        avg_msgs = round(total_msgs / total_sess, 1) if total_sess > 0 else 0.0

        # 安全拦截率
        total_safety = (
            await db.execute(
                select(func.count(SafetyLog.id))
                .where(SafetyLog.created_at >= since)
            )
        ).scalar() or 0
        blocks = (
            await db.execute(
                select(func.count(SafetyLog.id))
                .where(SafetyLog.verdict == "BLOCK")
                .where(SafetyLog.created_at >= since)
            )
        ).scalar() or 0
        block_rate = round(blocks / total_safety, 4) if total_safety > 0 else 0.0

        return OverviewOut(
            total_sessions=total_sess,
            active_sessions=active_sess,
            total_messages=total_msgs,
            avg_messages_per_session=avg_msgs,
            safety_block_rate=block_rate,
        )


@router.get("/trends")
async def analytics_trends(
    request: Request,
    days: int = Query(default=30, ge=1, le=365),
) -> list[TrendItem]:
    """按天趋势：会话数、消息数、推荐数。"""
    async with get_db() as db:
        since = datetime.now(timezone.utc) - timedelta(days=days)

        # 每日会话数
        sess_trend = (
            await db.execute(
                select(
                    func.date(SessionModel.created_at).label("day"),
                    func.count(SessionModel.id).label("sessions"),
                )
                .where(SessionModel.created_at >= since)
                .group_by("day")
            )
        ).all()
        sess_map = {str(r[0]): r[1] for r in sess_trend}

        # 每日消息数
        msg_trend = (
            await db.execute(
                select(
                    func.date(Message.created_at).label("day"),
                    func.count(Message.id).label("messages"),
                )
                .where(Message.created_at >= since)
                .group_by("day")
            )
        ).all()
        msg_map = {str(r[0]): r[1] for r in msg_trend}

        # 每日推荐数（有 recommendations 的会话）
        rec_trend = (
            await db.execute(
                select(
                    func.date(SessionModel.created_at).label("day"),
                    func.count(SessionModel.id),
                )
                .where(SessionModel.created_at >= since)
                .where(
                    SessionModel.state_snapshot.isnot(None)
                )
                .group_by("day")
            )
        ).all()
        rec_map = {str(r[0]): r[1] for r in rec_trend}

        # 构建时间序列
        items = []
        for i in range(days):
            d = (datetime.now(timezone.utc) - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
            items.append(
                TrendItem(
                    date=d,
                    sessions=sess_map.get(d, 0),
                    messages=msg_map.get(d, 0),
                    recommendations=rec_map.get(d, 0),
                )
            )

        return items


@router.get("/intents")
async def analytics_intents(
    request: Request,
    days: int = Query(default=30, ge=1, le=365),
) -> list[IntentItem]:
    """用户 Intent 分布。"""
    async with get_db() as db:
        since = datetime.now(timezone.utc) - timedelta(days=days)

        rows = (
            await db.execute(
                select(
                    Message.intent,
                    func.count(Message.id),
                )
                .where(Message.role == "user")
                .where(Message.intent.isnot(None))
                .where(Message.created_at >= since)
                .group_by(Message.intent)
                .order_by(func.count(Message.id).desc())
            )
        ).all()

        return [IntentItem(intent=r[0], count=r[1]) for r in rows]


@router.get("/conversion")
async def analytics_conversion(
    request: Request,
    days: int = Query(default=30, ge=1, le=365),
) -> ConversionFunnel:
    """转化漏斗：症状 → 推荐 → 完整流程。"""
    async with get_db() as db:
        since = datetime.now(timezone.utc) - timedelta(days=days)

        total = (
            await db.execute(
                select(func.count(SessionModel.id))
                .where(SessionModel.created_at >= since)
            )
        ).scalar() or 0

        # 有症状描述（含用户消息的 session）
        with_symptoms = (
            await db.execute(
                select(func.count(func.distinct(SessionModel.id)))
                .join(Message, SessionModel.id == Message.session_id)
                .where(Message.role == "user")
                .where(SessionModel.created_at >= since)
            )
        ).scalar() or 0

        # 有推荐结果的
        rec_given = (
            await db.execute(
                select(func.count(SessionModel.id))
                .where(SessionModel.state_snapshot.isnot(None))
                .where(SessionModel.created_at >= since)
            )
        ).scalar() or 0

        # 有 AI 回复的（含 assistant 消息的 session）
        with_ai_response = (
            await db.execute(
                select(func.count(func.distinct(SessionModel.id)))
                .join(Message, SessionModel.id == Message.session_id)
                .where(Message.role == "assistant")
                .where(SessionModel.created_at >= since)
            )
        ).scalar() or 0

        return ConversionFunnel(
            total_sessions=total,
            with_symptoms=with_symptoms,
            recommendations_given=rec_given,
            with_ai_response=with_ai_response,
        )


@router.get("/top-drugs")
async def analytics_top_drugs(
    request: Request,
    days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=10, ge=1, le=50),
) -> list[TopDrugItem]:
    """推荐最多的药品 Top N。

    从 sessions.state_snapshot 的 recommendations 中提取。
    """
    async with get_db() as db:
        since = datetime.now(timezone.utc) - timedelta(days=days)

        # 获取所有带推荐结果的 state_snapshot
        rows = (
            await db.execute(
                select(SessionModel.state_snapshot)
                .where(SessionModel.state_snapshot.isnot(None))
                .where(SessionModel.created_at >= since)
            )
        ).scalars().all()

        # 在 Python 中聚合（state_snapshot 是 JSON，SQL 内解析复杂）
        from collections import Counter

        counter: Counter = Counter()
        for snap in rows:
            if isinstance(snap, dict):
                recs = snap.get("recommendations", [])
                for r in recs:
                    if isinstance(r, dict):
                        name = r.get("generic_name", "")
                        if name:
                            counter[name] += 1

        return [
            TopDrugItem(drug_name=name, count=count)
            for name, count in counter.most_common(limit)
        ]
