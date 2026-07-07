"""
Admin 高风险关键字监控 — 关键字管理 + 告警处理。

GET    /api/v1/admin/risk-keywords        — 关键字列表
POST   /api/v1/admin/risk-keywords        — 新增关键字
PUT    /api/v1/admin/risk-keywords/{id}   — 编辑
DELETE /api/v1/admin/risk-keywords/{id}   — 删除

GET    /api/v1/admin/risk-alerts          — 告警列表
PUT    /api/v1/admin/risk-alerts/{id}/review — 标记已处理
GET    /api/v1/admin/risk-alerts/stats    — 告警统计
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.routes.admin.schemas import PaginatedResponse
from app.db.database import get_db
from app.db.models import HighRiskAlert, HighRiskKeyword

router = APIRouter(tags=["admin"])


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts else None


# ── Schema ──────────────────────────────────────────────────


class KeywordItem(BaseModel):
    id: int
    keyword: str
    category: str
    severity: str
    is_active: bool
    negative_patterns: str | None = None
    created_at: str | None


class KeywordCreate(BaseModel):
    keyword: str = Field(..., min_length=1, max_length=200)
    category: str = "other"
    severity: str = "medium"
    is_active: bool = True
    negative_patterns: str | None = Field(
        default=None, max_length=500,
        description="白名单正则（逗号分隔），命中时抑制告警。例如: '药品,解毒,消毒'"
    )


class AlertItem(BaseModel):
    id: int
    session_id: str
    keyword_id: int | None
    matched_content: str
    is_reviewed: bool
    reviewed_by: str | None
    review_notes: str | None
    created_at: str | None


class AlertStatsOut(BaseModel):
    total_alerts: int
    reviewed_count: int
    unreviewed_count: int
    by_category: dict
    by_severity: dict


# ── Keywords CRUD ──────────────────────────────────────────


@router.get("/risk-keywords")
async def list_keywords(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    category: str | None = Query(default=None),
    is_active: bool | None = Query(default=None),
) -> PaginatedResponse[KeywordItem]:
    """分页查询高风险关键字列表。"""
    async with get_db() as db:
        base = select(HighRiskKeyword).where(HighRiskKeyword.deleted_at.is_(None))
        if category:
            base = base.where(HighRiskKeyword.category == category)
        if is_active is not None:
            base = base.where(HighRiskKeyword.is_active == is_active)
        base = base.order_by(HighRiskKeyword.severity.desc(), HighRiskKeyword.keyword.asc())

        count_q = select(func.count()).select_from(base.subquery())
        total = (await db.execute(count_q)).scalar() or 0

        offset = (page - 1) * page_size
        rows = (
            await db.execute(base.offset(offset).limit(page_size))
        ).scalars().all()

        items = [
            KeywordItem(
                id=r.id, keyword=r.keyword, category=r.category,
                severity=r.severity, is_active=r.is_active,
                negative_patterns=getattr(r, 'negative_patterns', None),
                created_at=_iso(r.created_at),
            )
            for r in rows
        ]

        return PaginatedResponse(
            items=items, total=total, page=page, page_size=page_size,
        )


@router.post("/risk-keywords", status_code=201)
async def create_keyword(body: KeywordCreate) -> KeywordItem:
    """新增高风险关键字。

    可选设置 negative_patterns（白名单），当内容包含关键字但符合
    白名单模式时抑制告警（减少误匹配）。
    """
    async with get_db() as db:
        kw = HighRiskKeyword(
            keyword=body.keyword,
            category=body.category,
            severity=body.severity,
            is_active=body.is_active,
        )
        # 动态设置 negative_patterns（若模型有此字段）
        if hasattr(HighRiskKeyword, 'negative_patterns'):
            kw.negative_patterns = body.negative_patterns

        db.add(kw)
        await db.commit()
        await db.refresh(kw)
        return KeywordItem(
            id=kw.id, keyword=kw.keyword, category=kw.category,
            severity=kw.severity, is_active=kw.is_active,
            negative_patterns=getattr(kw, 'negative_patterns', None),
            created_at=_iso(kw.created_at),
        )


@router.put("/risk-keywords/{kw_id}")
async def update_keyword(kw_id: int, body: KeywordCreate) -> KeywordItem:
    """编辑关键字（已软删除的不可编辑）。"""
    async with get_db() as db:
        kw = (
            await db.execute(
                select(HighRiskKeyword).where(
                    HighRiskKeyword.id == kw_id,
                    HighRiskKeyword.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if kw is None:
            raise HTTPException(status_code=404, detail="Keyword not found")

        kw.keyword = body.keyword
        kw.category = body.category
        kw.severity = body.severity
        kw.is_active = body.is_active
        if hasattr(HighRiskKeyword, 'negative_patterns'):
            kw.negative_patterns = body.negative_patterns
        await db.commit()
        await db.refresh(kw)

        return KeywordItem(
            id=kw.id, keyword=kw.keyword, category=kw.category,
            severity=kw.severity, is_active=kw.is_active,
            negative_patterns=getattr(kw, 'negative_patterns', None),
            created_at=_iso(kw.created_at),
        )


@router.delete("/risk-keywords/{kw_id}")
async def delete_keyword(kw_id: int):
    """软删除关键字（设置 deleted_at），保留告警关联。"""
    async with get_db() as db:
        kw = (
            await db.execute(
                select(HighRiskKeyword).where(
                    HighRiskKeyword.id == kw_id,
                    HighRiskKeyword.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if kw is None:
            raise HTTPException(status_code=404, detail="Keyword not found")
        kw.deleted_at = datetime.now(timezone.utc)
        await db.commit()
        return {"success": True, "message": f"Keyword '{kw.keyword}' soft-deleted", "id": kw_id}


# ── Alerts ──────────────────────────────────────────────────


@router.get("/risk-alerts")
async def list_alerts(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    is_reviewed: bool | None = Query(default=None),
    category: str | None = Query(default=None),
) -> PaginatedResponse[AlertItem]:
    """分页查询告警列表。"""
    async with get_db() as db:
        base = select(HighRiskAlert)

        if is_reviewed is not None:
            base = base.where(HighRiskAlert.is_reviewed == is_reviewed)
        if category:
            base = base.where(
                HighRiskAlert.keyword_id.in_(
                    select(HighRiskKeyword.id).where(
                        HighRiskKeyword.category == category,
                        HighRiskKeyword.deleted_at.is_(None),
                    )
                )
            )

        base = base.order_by(HighRiskAlert.created_at.desc())

        count_q = select(func.count()).select_from(base.subquery())
        total = (await db.execute(count_q)).scalar() or 0

        offset = (page - 1) * page_size
        rows = (
            await db.execute(base.offset(offset).limit(page_size))
        ).scalars().all()

        items = [
            AlertItem(
                id=r.id, session_id=r.session_id,
                keyword_id=r.keyword_id,
                matched_content=r.matched_content,
                is_reviewed=r.is_reviewed,
                reviewed_by=r.reviewed_by,
                review_notes=r.review_notes,
                created_at=_iso(r.created_at),
            )
            for r in rows
        ]

        return PaginatedResponse(
            items=items, total=total, page=page, page_size=page_size,
        )


class ReviewBody(BaseModel):
    reviewed_by: str = "admin"
    review_notes: str = ""


@router.put("/risk-alerts/{alert_id}/review")
async def review_alert(
    alert_id: int,
    body: ReviewBody = ReviewBody(),
) -> dict:
    """标记告警为已处理。"""
    async with get_db() as db:
        alert = await db.get(HighRiskAlert, alert_id)
        if alert is None:
            raise HTTPException(status_code=404, detail="Alert not found")

        alert.is_reviewed = True
        alert.reviewed_by = body.reviewed_by
        alert.review_notes = body.review_notes
        await db.commit()

        return {"success": True, "alert_id": alert_id}


@router.get("/risk-alerts/stats")
async def alert_stats(days: int = Query(default=30, ge=1, le=365)) -> AlertStatsOut:
    """告警统计。"""
    async with get_db() as db:
        since = datetime.now(timezone.utc) - timedelta(days=days)

        total = (
            await db.execute(
                select(func.count(HighRiskAlert.id))
                .where(HighRiskAlert.created_at >= since)
            )
        ).scalar() or 0

        reviewed = (
            await db.execute(
                select(func.count(HighRiskAlert.id))
                .where(HighRiskAlert.is_reviewed == True)
                .where(HighRiskAlert.created_at >= since)
            )
        ).scalar() or 0

        # 按类别
        cat_rows = (
            await db.execute(
                select(
                    HighRiskKeyword.category,
                    func.count(HighRiskAlert.id),
                )
                .join(HighRiskKeyword, HighRiskAlert.keyword_id == HighRiskKeyword.id)
                .where(
                    HighRiskAlert.created_at >= since,
                    HighRiskKeyword.deleted_at.is_(None),
                )
                .group_by(HighRiskKeyword.category)
            )
        ).all()
        by_category = {r[0] or "unknown": r[1] for r in cat_rows}

        # 按严重程度
        sev_rows = (
            await db.execute(
                select(
                    HighRiskKeyword.severity,
                    func.count(HighRiskAlert.id),
                )
                .join(HighRiskKeyword, HighRiskAlert.keyword_id == HighRiskKeyword.id)
                .where(
                    HighRiskAlert.created_at >= since,
                    HighRiskKeyword.deleted_at.is_(None),
                )
                .group_by(HighRiskKeyword.severity)
            )
        ).all()
        by_severity = {r[0]: r[1] for r in sev_rows}

        return AlertStatsOut(
            total_alerts=total,
            reviewed_count=reviewed,
            unreviewed_count=total - reviewed,
            by_category=by_category,
            by_severity=by_severity,
        )
