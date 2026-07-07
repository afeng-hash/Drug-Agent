"""
Admin 反馈管理 — 用户反馈查看与统计。

GET  /api/v1/admin/feedback       — 分页列表
GET  /api/v1/admin/feedback/stats — 按药品聚合评分
"""

from datetime import datetime

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.routes.admin.schemas import PaginatedResponse
from app.db.database import get_db
from app.db.models import Feedback, Drug

router = APIRouter(prefix="/feedback", tags=["admin"])


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts else None


class FeedbackItem(BaseModel):
    id: int
    session_id: str
    drug_id: int | None
    drug_name: str | None
    rating: int
    comment: str | None
    created_at: str | None


class FeedbackStatsItem(BaseModel):
    drug_name: str
    avg_rating: float
    feedback_count: int


@router.get("")
async def list_feedback(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    drug_id: int | None = Query(default=None),
    rating: int | None = Query(default=None),
) -> PaginatedResponse[FeedbackItem]:
    """分页查询反馈列表。"""
    async with get_db() as db:
        base = select(Feedback)
        if drug_id is not None:
            base = base.where(Feedback.drug_id == drug_id)
        if rating is not None:
            base = base.where(Feedback.rating == rating)
        base = base.order_by(Feedback.created_at.desc())

        count_q = select(func.count()).select_from(base.subquery())
        total = (await db.execute(count_q)).scalar() or 0

        offset = (page - 1) * page_size
        rows = (
            await db.execute(base.offset(offset).limit(page_size))
        ).scalars().all()

        # 批量查询 drug names（一次查询替代 N 次）
        drug_ids = list({r.drug_id for r in rows if r.drug_id})
        drug_map: dict[int, str] = {}
        if drug_ids:
            drug_rows = (
                await db.execute(
                    select(Drug.id, Drug.generic_name).where(Drug.id.in_(drug_ids))
                )
            ).all()
            drug_map = {d[0]: d[1] for d in drug_rows}

        items = [
            FeedbackItem(
                id=r.id, session_id=r.session_id,
                drug_id=r.drug_id,
                drug_name=drug_map.get(r.drug_id) if r.drug_id else None,
                rating=r.rating, comment=r.comment,
                created_at=_iso(r.created_at),
            )
            for r in rows
        ]

        return PaginatedResponse(
            items=items, total=total, page=page, page_size=page_size,
        )


@router.get("/stats")
async def feedback_stats(
    limit: int = Query(default=20, ge=1, le=100),
) -> list[FeedbackStatsItem]:
    """按药品聚合反馈评分。"""
    async with get_db() as db:
        rows = (
            await db.execute(
                select(
                    Feedback.drug_id,
                    func.avg(Feedback.rating).label("avg_r"),
                    func.count(Feedback.id).label("cnt"),
                )
                .where(Feedback.drug_id.isnot(None))
                .group_by(Feedback.drug_id)
                .order_by(func.count(Feedback.id).desc())
                .limit(limit)
            )
        ).all()

        # 批量查询 drug names（一次查询替代 N 次）
        drug_ids = [r[0] for r in rows]
        drug_map: dict[int, str] = {}
        if drug_ids:
            drug_rows = (
                await db.execute(
                    select(Drug.id, Drug.generic_name).where(Drug.id.in_(drug_ids))
                )
            ).all()
            drug_map = {d[0]: d[1] for d in drug_rows}

        items = [
            FeedbackStatsItem(
                drug_name=drug_map.get(r[0], "Unknown"),
                avg_rating=round(r[1], 2),
                feedback_count=r[2],
            )
            for r in rows
        ]

        return items
