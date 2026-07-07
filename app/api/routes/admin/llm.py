"""
Admin LLM 管理 — 用量仪表盘 + 模型配置。

GET  /api/v1/admin/llm/overview  — 总量概览
GET  /api/v1/admin/llm/trends    — 时间趋势
GET  /api/v1/admin/llm/by-node   — 按节点分解
GET  /api/v1/admin/llm/calls     — 调用明细
GET  /api/v1/admin/llm/models    — 模型配置列表
PUT  /api/v1/admin/llm/models/{role} — 更新配置
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.routes.admin.schemas import PaginatedResponse
from app.db.database import get_db
from app.db.models import LLMCallLog, ModelConfig

router = APIRouter(prefix="/llm", tags=["admin"])


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts else None


# ── Schema ──────────────────────────────────────────────────


class LLMOverviewOut(BaseModel):
    total_calls: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    avg_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    error_rate: float = 0.0
    date_from: str | None = None
    date_to: str | None = None


class LLMTrendItem(BaseModel):
    date: str
    calls: int
    prompt_tokens: int
    completion_tokens: int


class LLMNodeBreakdown(BaseModel):
    node: str
    calls: int
    prompt_tokens: int
    completion_tokens: int
    avg_latency_ms: float


class LLMCallLogItem(BaseModel):
    id: int
    session_id: str | None
    node: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float
    success: bool
    error_message: str | None
    created_at: str | None


class ModelConfigItem(BaseModel):
    id: int
    role: str
    model_name: str
    temperature: float
    max_tokens: int
    is_active: bool
    description: str
    updated_at: str | None


class ModelConfigUpdate(BaseModel):
    model_name: str | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1)
    description: str | None = None


# ── 缓存 ────────────────────────────────────────────────────

_model_config_cache: dict[str, ModelConfigItem] = {}


async def _ensure_cache(db) -> dict[str, ModelConfigItem]:
    """确保模型配置缓存已加载。"""
    global _model_config_cache
    if not _model_config_cache:
        rows = (
            await db.execute(
                select(ModelConfig).where(ModelConfig.is_active == True)
            )
        ).scalars().all()
        _model_config_cache = {
            r.role: ModelConfigItem(
                id=r.id,
                role=r.role,
                model_name=r.model_name,
                temperature=r.temperature,
                max_tokens=r.max_tokens,
                is_active=r.is_active,
                description=r.description,
                updated_at=_iso(r.updated_at),
            )
            for r in rows
        }
    return _model_config_cache


# ── LLM 用量 ────────────────────────────────────────────────


def _parse_date_range(date_from: str | None, date_to: str | None):
    """解析日期范围，返回 (datetime | None, datetime | None)。"""
    df = datetime.fromisoformat(date_from) if date_from else None
    dt = datetime.fromisoformat(date_to) if date_to else None
    return df, dt


@router.get("/overview")
async def llm_overview(
    request: Request,
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
) -> LLMOverviewOut:
    """LLM 用量总量概览。"""
    async with get_db() as db:
        df, dt = _parse_date_range(date_from, date_to)

        base = select(LLMCallLog)
        if df:
            base = base.where(LLMCallLog.created_at >= df)
        if dt:
            base = base.where(LLMCallLog.created_at <= dt)

        # 总量
        stats = (
            await db.execute(
                select(
                    func.count(LLMCallLog.id),
                    func.coalesce(func.sum(LLMCallLog.prompt_tokens), 0),
                    func.coalesce(func.sum(LLMCallLog.completion_tokens), 0),
                    func.coalesce(func.avg(LLMCallLog.latency_ms), 0.0),
                    func.count(LLMCallLog.id).filter(LLMCallLog.success == False),
                ).select_from(base.subquery())
            )
        ).one()

        total = stats[0]
        err_count = stats[4]
        error_rate = round(err_count / total, 4) if total > 0 else 0.0

        # P95 延迟（简单近似：取前 95% 内的最大值）
        p95 = 0.0
        if total > 0:
            p95_rows = (
                await db.execute(
                    select(LLMCallLog.latency_ms)
                    .order_by(LLMCallLog.latency_ms.desc())
                    .limit(max(1, int(total * 0.05)))
                )
            ).all()
            if p95_rows:
                p95 = p95_rows[-1][0]

        return LLMOverviewOut(
            total_calls=total,
            total_prompt_tokens=stats[1],
            total_completion_tokens=stats[2],
            avg_latency_ms=round(stats[3], 1),
            p95_latency_ms=round(p95, 1),
            error_rate=error_rate,
            date_from=date_from,
            date_to=date_to,
        )


@router.get("/trends")
async def llm_trends(
    request: Request,
    days: int = Query(default=7, ge=1, le=90),
) -> list[LLMTrendItem]:
    """LLM 用量按天趋势。"""
    async with get_db() as db:
        since = datetime.now(timezone.utc) - timedelta(days=days)

        rows = (
            await db.execute(
                select(
                    func.date(LLMCallLog.created_at).label("day"),
                    func.count(LLMCallLog.id).label("calls"),
                    func.coalesce(func.sum(LLMCallLog.prompt_tokens), 0),
                    func.coalesce(func.sum(LLMCallLog.completion_tokens), 0),
                )
                .where(LLMCallLog.created_at >= since)
                .group_by("day")
                .order_by("day")
            )
        ).all()

        return [
            LLMTrendItem(
                date=str(r[0]),
                calls=r[1],
                prompt_tokens=r[2],
                completion_tokens=r[3],
            )
            for r in rows
        ]


@router.get("/by-node")
async def llm_by_node(
    request: Request,
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
) -> list[LLMNodeBreakdown]:
    """按调用节点分解用量。"""
    async with get_db() as db:
        df, dt = _parse_date_range(date_from, date_to)

        base = select(
            LLMCallLog.node,
            func.count(LLMCallLog.id).label("calls"),
            func.coalesce(func.sum(LLMCallLog.prompt_tokens), 0),
            func.coalesce(func.sum(LLMCallLog.completion_tokens), 0),
            func.coalesce(func.avg(LLMCallLog.latency_ms), 0.0),
        ).group_by(LLMCallLog.node)

        if df:
            base = base.where(LLMCallLog.created_at >= df)
        if dt:
            base = base.where(LLMCallLog.created_at <= dt)

        rows = (await db.execute(base.order_by(func.count(LLMCallLog.id).desc()))).all()

        return [
            LLMNodeBreakdown(
                node=r[0],
                calls=r[1],
                prompt_tokens=r[2],
                completion_tokens=r[3],
                avg_latency_ms=round(r[4], 1),
            )
            for r in rows
        ]


@router.get("/calls")
async def llm_calls(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    session_id: str | None = Query(default=None),
    node: str | None = Query(default=None),
    model: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
) -> PaginatedResponse[LLMCallLogItem]:
    """LLM 调用明细列表。"""
    async with get_db() as db:
        df, dt = _parse_date_range(date_from, date_to)

        base = select(LLMCallLog)
        if session_id:
            base = base.where(LLMCallLog.session_id == session_id)
        if node:
            base = base.where(LLMCallLog.node == node)
        if model:
            base = base.where(LLMCallLog.model == model)
        if df:
            base = base.where(LLMCallLog.created_at >= df)
        if dt:
            base = base.where(LLMCallLog.created_at <= dt)

        count_q = select(func.count()).select_from(base.subquery())
        total = (await db.execute(count_q)).scalar() or 0

        offset = (page - 1) * page_size
        rows = (
            await db.execute(
                base.order_by(LLMCallLog.created_at.desc()).offset(offset).limit(page_size)
            )
        ).scalars().all()

        items = [
            LLMCallLogItem(
                id=r.id,
                session_id=r.session_id,
                node=r.node,
                model=r.model,
                prompt_tokens=r.prompt_tokens,
                completion_tokens=r.completion_tokens,
                latency_ms=r.latency_ms,
                success=r.success,
                error_message=r.error_message,
                created_at=_iso(r.created_at),
            )
            for r in rows
        ]

        return PaginatedResponse(
            items=items, total=total, page=page, page_size=page_size,
        )


# ── 模型配置 ─────────────────────────────────────────────────


@router.get("/models")
async def list_model_configs(request: Request) -> list[ModelConfigItem]:
    """获取所有激活的模型配置。"""
    async with get_db() as db:
        cache = await _ensure_cache(db)
        return list(cache.values())


@router.put("/models/{role}")
async def update_model_config(
    role: str,
    body: ModelConfigUpdate,
    request: Request,
) -> ModelConfigItem:
    """更新并热加载模型配置。"""
    async with get_db() as db:
        stmt = select(ModelConfig).where(
            ModelConfig.role == role, ModelConfig.is_active == True
        )
        result = await db.execute(stmt)
        config = result.scalar_one_or_none()

        if config is None:
            raise HTTPException(status_code=404, detail=f"Model config not found: {role}")

        if body.model_name is not None:
            config.model_name = body.model_name
        if body.temperature is not None:
            config.temperature = body.temperature
        if body.max_tokens is not None:
            config.max_tokens = body.max_tokens
        if body.description is not None:
            config.description = body.description

        config.updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(config)

        # 更新缓存
        item = ModelConfigItem(
            id=config.id,
            role=config.role,
            model_name=config.model_name,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            is_active=config.is_active,
            description=config.description,
            updated_at=_iso(config.updated_at),
        )
        _model_config_cache[role] = item

        return item
