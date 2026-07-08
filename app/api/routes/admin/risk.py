"""
Admin 高风险关键字监控 — 关键字管理 + 告警处理

============================================================
模块概述：
  本模块提供药品咨询系统中的高风险关键字监控功能，用于实时识别和
  预警用户对话中的潜在风险内容（如违禁药品、危险用药建议等）。
  管理员可以通过 API 管理风险关键字库，查看告警记录，并对告警
  进行审核处理。

    【数据模型关系】
    HighRiskKeyword（关键字表） 1 ── N HighRiskAlert（告警表）
    每条告警关联一个关键字，当对话内容匹配到关键字时触发告警。

    【关键字管理功能】
    - 支持关键字的增删改查，使用软删除保留历史关联
    - 每个关键字可设置类别、严重程度、白名单正则（减少误报）

    【告警管理功能】
    - 分页查询告警列表，支持按类别、处理状态筛选
    - 标记告警为已处理，记录处理人和备注
    - 提供告警统计面板（按类别、严重程度聚合）

    【API 端点列表】
    GET    /api/v1/admin/risk-keywords           — 关键字列表（分页）
    POST   /api/v1/admin/risk-keywords           — 新增关键字
    PUT    /api/v1/admin/risk-keywords/{id}      — 编辑关键字
    DELETE /api/v1/admin/risk-keywords/{id}      — 软删除关键字

    GET    /api/v1/admin/risk-alerts             — 告警列表（分页）
    PUT    /api/v1/admin/risk-alerts/{id}/review — 标记告警为已处理
    GET    /api/v1/admin/risk-alerts/stats       — 告警统计
============================================================
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.routes.admin.schemas import PaginatedResponse
from app.db.database import get_db
from app.db.models import HighRiskAlert, HighRiskKeyword

# 创建 Admin 路由实例，在 OpenAPI 文档中归入 "admin" 分组
router = APIRouter(tags=["admin"])


def _iso(ts: datetime | None) -> str | None:
    """将 datetime 对象转换为 ISO 8601 格式字符串，用于 API 响应序列化。

    参数:
        ts (datetime | None): 待转换的时间戳，可以为 None。

    返回:
        str | None: ISO 格式时间字符串（如 "2026-07-07T12:30:00"），
                    如果传入 None 则返回 None。
    """
    return ts.isoformat() if ts else None


# ── Schema（请求/响应数据模型） ──────────────────────────────
# 以下 Pydantic 模型定义了各 API 端点的请求体和响应体结构


class KeywordItem(BaseModel):
    """高风险关键字的响应模型 — 返回给前端的关键字完整信息。

    字段:
        id (int):              关键字唯一标识 ID
        keyword (str):         关键字文本内容
        category (str):        关键字所属类别（如 "drug"、"behavior"）
        severity (str):        严重程度等级（"low" / "medium" / "high" / "critical"）
        is_active (bool):      是否启用，False 时该关键字不参与匹配
        negative_patterns (str | None): 白名单正则表达式（逗号分隔），
                                         命中时抑制告警以减少误报
        created_at (str | None): 创建时间（ISO 8601 格式）
    """
    id: int
    keyword: str
    category: str
    severity: str
    is_active: bool
    negative_patterns: str | None = None
    created_at: str | None


class KeywordCreate(BaseModel):
    """高风险关键字的创建/编辑请求模型 — 用于 POST 和 PUT 请求体。

    字段:
        keyword (str):          关键字文本，1-200 个字符，不允许为空
        category (str):         类别标签，默认为 "other"
        severity (str):         严重程度，默认为 "medium"
        is_active (bool):       是否启用，默认为 True
        negative_patterns (str | None): 白名单正则表达式（逗号分隔），
                                         最长 500 字符。
                                         例如: '药品,解毒,消毒'，
                                         当匹配内容也命中这些模式时，抑制告警。
    """
    keyword: str = Field(..., min_length=1, max_length=200)
    category: str = "other"
    severity: str = "medium"
    is_active: bool = True
    negative_patterns: str | None = Field(
        default=None, max_length=500,
        description="白名单正则（逗号分隔），命中时抑制告警。例如: '药品,解毒,消毒'"
    )


class AlertItem(BaseModel):
    """高风险告警的响应模型 — 返回给前端的单条告警完整信息。

    字段:
        id (int):                 告警唯一标识 ID
        session_id (str):         触发告警的对话会话 ID，用于追溯上下文
        keyword_id (int | None):  关联的关键字 ID，可能为 None（关键字已被删除）
        matched_content (str):    触发告警的原始对话内容片段
        is_reviewed (bool):       是否已处理（管理员已审核）
        reviewed_by (str | None): 处理人名称，未处理时为 None
        review_notes (str | None): 处理备注，未处理时为 None
        created_at (str | None):  告警创建时间（ISO 8601 格式）
    """
    id: int
    session_id: str
    keyword_id: int | None
    matched_content: str
    is_reviewed: bool
    reviewed_by: str | None
    review_notes: str | None
    created_at: str | None


class AlertStatsOut(BaseModel):
    """告警统计的响应模型 — 返回告警的聚合统计数据。

    字段:
        total_alerts (int):     统计周期内的告警总数
        reviewed_count (int):   已处理的告警数量
        unreviewed_count (int): 未处理的告警数量
        by_category (dict):     按关键字类别分组的告警数量，如 {"drug": 15, "behavior": 3}
        by_severity (dict):     按严重程度分组的告警数量，如 {"high": 8, "medium": 10}
    """
    total_alerts: int
    reviewed_count: int
    unreviewed_count: int
    by_category: dict
    by_severity: dict


# ── 关键字 CRUD（增删改查） ────────────────────────────────


@router.get("/risk-keywords")
async def list_keywords(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    category: str | None = Query(default=None),
    is_active: bool | None = Query(default=None),
) -> PaginatedResponse[KeywordItem]:
    """分页查询高风险关键字列表。

    这一步的作用：
      获取当前系统中所有活跃的高风险关键字，支持按类别和启用状态筛选。
      管理员使用此接口浏览和管理关键字库。

    参数:
        page (int):       当前页码，从 1 开始，最小值为 1
        page_size (int):  每页条数，默认 50 条，范围 1-200
        category (str | None): 按类别筛选，为 None 时不过滤类别。
                               如 "drug" 表示仅查询药品类关键字
        is_active (bool | None): 按启用状态筛选，None=全部，
                                 True=仅启用，False=仅禁用

    返回:
        PaginatedResponse[KeywordItem]: 分页后的关键字列表，
        包含 items（关键字数组）、total（总数）、page（当前页）、
        page_size（每页条数）
    """
    async with get_db() as db:
        # 构建基础查询：只查询未被软删除的关键字（deleted_at IS NULL）
        base = select(HighRiskKeyword).where(HighRiskKeyword.deleted_at.is_(None))
        # 如果指定了类别筛选条件，添加到 WHERE 子句
        if category:
            base = base.where(HighRiskKeyword.category == category)
        # 如果指定了启用状态筛选条件，添加到 WHERE 子句
        if is_active is not None:
            base = base.where(HighRiskKeyword.is_active == is_active)
        # 按严重程度降序、关键字名称升序排列（严重的排前面）
        base = base.order_by(HighRiskKeyword.severity.desc(), HighRiskKeyword.keyword.asc())

        # 统计符合条件的总记录数（用于前端分页组件计算总页数）
        count_q = select(func.count()).select_from(base.subquery())
        total = (await db.execute(count_q)).scalar() or 0

        # 计算当前页的偏移量，进行分页查询
        offset = (page - 1) * page_size
        rows = (
            await db.execute(base.offset(offset).limit(page_size))
        ).scalars().all()

        # 将 ORM 对象转换为 Pydantic 响应模型
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

    这一步的作用：
      在系统中添加一个新的高风险关键字，用于后续的对话内容匹配和告警。
      可选设置 negative_patterns（白名单正则）来减少误报 ——
      当对话内容同时命中关键字和白名单模式时，系统将抑制告警。

    参数:
        body (KeywordCreate): 新关键字的创建请求体，包含 keyword（关键字文本）、
                              category（类别）、severity（严重程度）、
                              is_active（是否启用）、negative_patterns（白名单正则）

    返回:
        KeywordItem: 创建成功后的关键字完整信息，包含服务端生成的 id 和 created_at
    """
    async with get_db() as db:
        # 构造新的 HighRiskKeyword ORM 对象
        kw = HighRiskKeyword(
            keyword=body.keyword,
            category=body.category,
            severity=body.severity,
            is_active=body.is_active,
        )
        # 动态设置 negative_patterns：仅当 ORM 模型包含此字段时才赋值
        # 使用 hasattr 检测字段存在性，保证向后兼容
        if hasattr(HighRiskKeyword, 'negative_patterns'):
            kw.negative_patterns = body.negative_patterns

        # 将新记录添加到数据库会话并提交
        db.add(kw)
        await db.commit()
        # 刷新对象以获取数据库生成的字段值（如 id、created_at）
        await db.refresh(kw)
        return KeywordItem(
            id=kw.id, keyword=kw.keyword, category=kw.category,
            severity=kw.severity, is_active=kw.is_active,
            negative_patterns=getattr(kw, 'negative_patterns', None),
            created_at=_iso(kw.created_at),
        )


@router.put("/risk-keywords/{kw_id}")
async def update_keyword(kw_id: int, body: KeywordCreate) -> KeywordItem:
    """编辑关键字。

    这一步的作用：
      更新指定关键字的所有字段。只能编辑未软删除的关键字，
      已被软删除的关键字无法编辑，需先恢复或重新创建。

    参数:
        kw_id (int):          要编辑的关键字 ID（路径参数）
        body (KeywordCreate): 新的关键字数据，包含所有可修改字段

    返回:
        KeywordItem: 更新后的关键字完整信息

    异常:
        HTTPException(404): 关键字不存在或已被软删除时抛出
    """
    async with get_db() as db:
        # 查询目标关键字：必须 ID 匹配且未被软删除
        kw = (
            await db.execute(
                select(HighRiskKeyword).where(
                    HighRiskKeyword.id == kw_id,
                    HighRiskKeyword.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        # 如果查不到记录，则关键字不存在或已被删除
        if kw is None:
            raise HTTPException(status_code=404, detail="Keyword not found")

        # 逐字段更新关键字的各个属性
        kw.keyword = body.keyword
        kw.category = body.category
        kw.severity = body.severity
        kw.is_active = body.is_active
        # 如果模型支持白名单正则字段，同步更新
        if hasattr(HighRiskKeyword, 'negative_patterns'):
            kw.negative_patterns = body.negative_patterns
        # 提交事务并刷新对象
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
    """软删除关键字。

    这一步的作用：
      通过设置 deleted_at 时间戳实现软删除，而非物理删除记录。
      这样做的好处是：
      - 保留历史告警与关键字的关联关系，不会出现"脏数据"
      - 管理员可以追溯已删除关键字曾经触发的告警
      - 删除后该关键字不再参与新的内容匹配（查询时会过滤 deleted_at IS NOT NULL）

    参数:
        kw_id (int): 要删除的关键字 ID（路径参数）

    返回:
        dict: 包含 success（操作结果）、message（描述信息）、
              id（被删除的关键字 ID）

    异常:
        HTTPException(404): 关键字不存在或已被软删除时抛出
    """
    async with get_db() as db:
        # 查询目标关键字：仅操作未被软删除的记录，防止重复删除
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
        # 设置删除时间戳为当前 UTC 时间，实现软删除
        kw.deleted_at = datetime.now(timezone.utc)
        await db.commit()
        return {"success": True, "message": f"Keyword '{kw.keyword}' soft-deleted", "id": kw_id}


# ── 告警管理 ────────────────────────────────────────────────


@router.get("/risk-alerts")
async def list_alerts(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    is_reviewed: bool | None = Query(default=None),
    category: str | None = Query(default=None),
) -> PaginatedResponse[AlertItem]:
    """分页查询告警列表。

    这一步的作用：
      获取系统中所有高风险告警记录，支持按处理状态和关键字类别筛选。
      管理员使用此接口查看待处理的告警，也可以查看已处理的历史记录。

    参数:
        page (int):         当前页码，从 1 开始，最小值为 1
        page_size (int):    每页条数，默认 20 条，范围 1-100
        is_reviewed (bool | None): 按处理状态筛选。None=全部，True=仅已处理，
                                    False=仅未处理
        category (str | None): 按关键字类别筛选，如 "drug"。
                               为 None 时不过滤类别

    返回:
        PaginatedResponse[AlertItem]: 分页后的告警列表，包含 items（告警数组）、
        total（总数）、page（当前页）、page_size（每页条数）
    """
    async with get_db() as db:
        # 基础查询：查询所有告警记录
        base = select(HighRiskAlert)

        # 按处理状态筛选：如果指定了 is_reviewed 参数，添加 WHERE 条件
        if is_reviewed is not None:
            base = base.where(HighRiskAlert.is_reviewed == is_reviewed)
        # 按关键字类别筛选：通过子查询找到属于指定类别的关键字 ID
        # 然后筛选出关联这些关键字的告警
        if category:
            base = base.where(
                HighRiskAlert.keyword_id.in_(
                    select(HighRiskKeyword.id).where(
                        HighRiskKeyword.category == category,
                        HighRiskKeyword.deleted_at.is_(None),
                    )
                )
            )

        # 按创建时间倒序排列，最新告警排在前面
        base = base.order_by(HighRiskAlert.created_at.desc())

        # 统计符合条件的总记录数
        count_q = select(func.count()).select_from(base.subquery())
        total = (await db.execute(count_q)).scalar() or 0

        # 计算偏移量，进行分页查询
        offset = (page - 1) * page_size
        rows = (
            await db.execute(base.offset(offset).limit(page_size))
        ).scalars().all()

        # 将 ORM 对象转换为 Pydantic 响应模型
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
    """告警处理请求体 — 标记告警为已处理时提交的数据。

    字段:
        reviewed_by (str):  处理人名称/ID，默认为 "admin"
        review_notes (str): 处理备注说明，默认为空字符串
    """
    reviewed_by: str = "admin"
    review_notes: str = ""


@router.put("/risk-alerts/{alert_id}/review")
async def review_alert(
    alert_id: int,
    body: ReviewBody = ReviewBody(),
) -> dict:
    """标记告警为已处理。

    这一步的作用：
      管理员对一条告警进行审核后，将其标记为"已处理"状态，
      同时记录处理人和备注信息。处理后的告警会从"待处理"列表中移除，
      但保留在历史记录中供审计查询。

    参数:
        alert_id (int):     要处理的告警 ID（路径参数）
        body (ReviewBody):  处理信息，包含处理人名称和处理备注。
                            默认为 reviewed_by="admin", review_notes=""

    返回:
        dict: 包含 success（操作结果）、alert_id（被处理的告警 ID）

    异常:
        HTTPException(404): 告警不存在时抛出
    """
    async with get_db() as db:
        # 通过主键直接获取告警记录
        alert = await db.get(HighRiskAlert, alert_id)
        if alert is None:
            raise HTTPException(status_code=404, detail="Alert not found")

        # 更新告警状态为"已处理"，并记录处理人和备注
        alert.is_reviewed = True
        alert.reviewed_by = body.reviewed_by
        alert.review_notes = body.review_notes
        await db.commit()

        return {"success": True, "alert_id": alert_id}


@router.get("/risk-alerts/stats")
async def alert_stats(days: int = Query(default=30, ge=1, le=365)) -> AlertStatsOut:
    """告警统计 —— 获取指定时间范围内的告警聚合数据。

    这一步的作用：
      为管理员提供告警的全局统计视图，包括总量、处理率、
      以及按类别和严重程度的分布情况。可用于生成监控仪表盘和趋势分析。

    参数:
        days (int): 统计的时间范围（天数），默认 30 天，范围 1-365。
                    例如 days=7 表示统计最近 7 天的告警数据

    返回:
        AlertStatsOut: 包含以下聚合统计数据的对象：
          - total_alerts:     统计周期内的告警总数
          - reviewed_count:   已处理告警数量
          - unreviewed_count: 未处理告警数量
          - by_category:      按关键字类别分组的数量，如 {"drug": 15, "other": 5}
          - by_severity:      按严重程度分组的数量，如 {"high": 8, "medium": 10, "low": 2}
    """
    async with get_db() as db:
        # 计算统计的起始时间：当前 UTC 时间减去指定天数
        since = datetime.now(timezone.utc) - timedelta(days=days)

        # --- 统计 1：时间范围内的告警总数 ---
        total = (
            await db.execute(
                select(func.count(HighRiskAlert.id))
                .where(HighRiskAlert.created_at >= since)
            )
        ).scalar() or 0

        # --- 统计 2：时间范围内已处理的告警数 ---
        reviewed = (
            await db.execute(
                select(func.count(HighRiskAlert.id))
                .where(HighRiskAlert.is_reviewed == True)
                .where(HighRiskAlert.created_at >= since)
            )
        ).scalar() or 0

        # --- 统计 3：按关键字类别分组统计告警数量 ---
        # 通过 JOIN 关键字表获取类别名称，按类别分组计数
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
        # 将查询结果转为字典，无类别的告警归入 "unknown"
        by_category = {r[0] or "unknown": r[1] for r in cat_rows}

        # --- 统计 4：按严重程度分组统计告警数量 ---
        # 与类别统计类似，按严重程度（low/medium/high/critical）分组计数
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
