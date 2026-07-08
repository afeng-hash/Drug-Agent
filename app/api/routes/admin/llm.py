"""
Admin LLM 管理模块 — 提供 LLM 用量仪表盘 + 模型配置管理功能。

本模块是后台管理系统中 LLM（大语言模型）相关的核心接口，主要承担两大职责：
  1. LLM 用量监控：统计和分析 LLM 调用次数、Token 消耗、延迟、错误率等关键指标，
     帮助运维人员了解系统运行状况和成本。
  2. 模型配置管理：查询和动态更新各角色使用的 LLM 模型参数（模型名、温度、最大 Token 数等），
     支持运行时热加载，无需重启服务。

接口列表：
  GET  /api/v1/admin/llm/overview    — LLM 调用总量概览（总次数、Token、延迟、错误率）
  GET  /api/v1/admin/llm/trends      — 按天统计的调用趋势（支持 1~90 天范围）
  GET  /api/v1/admin/llm/by-node     — 按调用节点（功能模块）分解用量
  GET  /api/v1/admin/llm/calls       — LLM 调用明细列表（分页、筛选、排序）
  GET  /api/v1/admin/llm/models      — 获取当前所有激活的模型配置列表
  PUT  /api/v1/admin/llm/models/{role} — 更新指定角色（role）的模型配置并热加载缓存
"""

# ── 标准库 ──────────────────────────────────────────────────
from datetime import datetime, timedelta, timezone

# ── 第三方库 ────────────────────────────────────────────────
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

# ── 项目内部模块 ────────────────────────────────────────────
from app.api.routes.admin.schemas import PaginatedResponse
from app.db.database import get_db
from app.db.models import LLMCallLog, ModelConfig

# 创建路由实例，前缀为 /llm，属于 admin 标签分组
router = APIRouter(prefix="/llm", tags=["admin"])


def _iso(ts: datetime | None) -> str | None:
    """将 datetime 对象转换为 ISO 8601 格式字符串。

    用于 API 响应中统一时间格式输出。

    参数：
        ts (datetime | None): 待转换的时间对象，可以为 None。

    返回：
        str | None: ISO 8601 格式的时间字符串（如 "2025-01-15T10:30:00"），
                    如果 ts 为 None 则返回 None。
    """
    return ts.isoformat() if ts else None


# ── 请求/响应 Schema 定义 ──────────────────────────────────
# 以下 Pydantic 模型定义了各接口的请求参数校验和响应数据结构


class LLMOverviewOut(BaseModel):
    """LLM 用量总量概览响应模型。

    汇总指定时间范围内所有 LLM 调用的核心指标，用于仪表盘首页展示。
    """
    total_calls: int = 0              # 总调用次数
    total_prompt_tokens: int = 0      # 输入 Token 总量（prompt tokens）
    total_completion_tokens: int = 0  # 输出 Token 总量（completion tokens）
    avg_latency_ms: float = 0.0       # 平均延迟（毫秒）
    p95_latency_ms: float = 0.0       # P95 延迟（毫秒）：95% 的请求延迟低于此值
    error_rate: float = 0.0           # 错误率：失败调用数 / 总调用数
    date_from: str | None = None      # 统计起始日期（ISO 格式字符串）
    date_to: str | None = None        # 统计截止日期（ISO 格式字符串）


class LLMTrendItem(BaseModel):
    """LLM 单日趋势数据条目。

    用于折线图/柱状图展示每日的调用量和 Token 消耗趋势。
    """
    date: str                        # 日期（格式：YYYY-MM-DD）
    calls: int                       # 当日调用次数
    prompt_tokens: int               # 当日输入 Token 总量
    completion_tokens: int           # 当日输出 Token 总量


class LLMNodeBreakdown(BaseModel):
    """LLM 按调用节点的用量分解条目。

    展示各个功能模块（如 Consult、Explain、Dispatch 等）分别消耗的 LLM 资源，
    便于定位成本热点。
    """
    node: str                        # 调用节点名称（标识是哪个功能模块发起的 LLM 请求）
    calls: int                       # 该节点的调用次数
    prompt_tokens: int               # 该节点的输入 Token 总量
    completion_tokens: int           # 该节点的输出 Token 总量
    avg_latency_ms: float            # 该节点的平均响应延迟（毫秒）


class LLMCallLogItem(BaseModel):
    """LLM 单条调用记录详情。

    用于分页列表展示每次 LLM 调用的详细信息，支持按会话、节点、模型筛选。
    """
    id: int                          # 调用记录主键 ID
    session_id: str | None           # 所属会话 ID（关联用户对话）
    node: str                        # 发起调用的节点名称
    model: str                       # 使用的模型名称（如 gpt-4、claude-3）
    prompt_tokens: int               # 本次调用消耗的输入 Token 数
    completion_tokens: int           # 本次调用消耗的输出 Token 数
    latency_ms: float                # 本次调用的响应延迟（毫秒）
    success: bool                    # 调用是否成功
    error_message: str | None        # 失败时的错误信息
    created_at: str | None           # 调用发生时间（ISO 格式）


class ModelConfigItem(BaseModel):
    """模型配置条目响应模型。

    表示某个角色（role）对应的 LLM 模型配置信息。
    """
    id: int                          # 配置记录主键 ID
    role: str                        # 角色名称（如 default、consult、explain、dispatch）
    model_name: str                  # 模型名称（如 gpt-4o、claude-sonnet-4）
    temperature: float               # 温度参数：控制输出随机性，范围 0.0~2.0
    max_tokens: int                  # 单次请求最大输出 Token 数
    is_active: bool                  # 是否处于激活状态
    description: str                 # 配置描述信息
    updated_at: str | None           # 最后更新时间（ISO 格式）


class ModelConfigUpdate(BaseModel):
    """更新模型配置的请求体。

    所有字段均为可选：只更新用户显式提供的字段，未提供的字段保持原值不变。
    """
    model_name: str | None = None                             # 新的模型名称
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)  # 温度参数，有效范围 [0.0, 2.0]
    max_tokens: int | None = Field(default=None, ge=1)        # 最大 Token 数，必须 >= 1
    description: str | None = None                            # 新的描述信息


# ── 模型配置内存缓存 ─────────────────────────────────────────
# 使用模块级字典缓存活跃的模型配置，避免每次请求都查询数据库。
# Key 为角色名（role），Value 为对应的 ModelConfigItem。
# 首次访问时从数据库加载，后续通过 PUT 接口更新时同步刷新。
_model_config_cache: dict[str, ModelConfigItem] = {}


async def _ensure_cache(db) -> dict[str, ModelConfigItem]:
    """确保模型配置缓存已加载。

    这是一个懒加载（lazy-load）方法：首次调用时从数据库查询所有激活的模型配置，
    填充到内存缓存中；之后的调用直接返回缓存数据，避免重复数据库查询。

    参数：
        db: 数据库会话对象（AsyncSession），用于执行查询。

    返回：
        dict[str, ModelConfigItem]: 以 role 为键、ModelConfigItem 为值的缓存字典。
    """
    global _model_config_cache
    # 如果缓存为空，说明尚未初始化，需要从数据库加载
    if not _model_config_cache:
        # 查询所有 is_active=True 的模型配置
        rows = (
            await db.execute(
                select(ModelConfig).where(ModelConfig.is_active == True)
            )
        ).scalars().all()
        # 将 ORM 对象转换为 Pydantic Schema 并存入缓存
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


# ── LLM 用量统计接口 ────────────────────────────────────────
# 提供 LLM 调用的多维度统计分析：总量概览、趋势、节点分解、明细查询


def _parse_date_range(date_from: str | None, date_to: str | None):
    """解析日期范围字符串为 datetime 对象。

    将前端传入的 ISO 格式日期字符串解析为 Python datetime 对象，
    用于后续数据库查询的日期过滤。

    参数：
        date_from (str | None): 起始日期（ISO 格式，如 "2025-01-01"）。
        date_to (str | None): 截止日期（ISO 格式，如 "2025-01-31"）。

    返回：
        tuple[datetime | None, datetime | None]: (起始时间, 截止时间)
    """
    df = datetime.fromisoformat(date_from) if date_from else None
    dt = datetime.fromisoformat(date_to) if date_to else None
    return df, dt


@router.get("/overview")
async def llm_overview(
    request: Request,
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
) -> LLMOverviewOut:
    """获取 LLM 用量总量概览。

    汇总指定时间范围内的核心指标：总调用次数、Token 消耗、平均/P95 延迟、错误率。
    用于管理后台仪表盘首页的关键数据卡片展示。

    参数：
        request (Request): FastAPI 请求对象（框架自动注入）。
        date_from (str | None): 可选，统计起始日期（ISO 格式，如 "2025-01-01"）。
        date_to (str | None): 可选，统计截止日期（ISO 格式，如 "2025-01-31"）。

    返回：
        LLMOverviewOut: 包含总调用次数、Token 总量、延迟数据和错误率的概览对象。
    """
    async with get_db() as db:
        # 解析日期过滤条件
        df, dt = _parse_date_range(date_from, date_to)

        # 构建基础查询：从 LLMCallLog 表中选取数据
        base = select(LLMCallLog)
        # 如果指定了起始日期，添加 created_at >= df 的条件
        if df:
            base = base.where(LLMCallLog.created_at >= df)
        # 如果指定了截止日期，添加 created_at <= dt 的条件
        if dt:
            base = base.where(LLMCallLog.created_at <= dt)

        # 执行聚合统计查询，一次 SQL 获取 5 个核心指标
        # func.coalesce 用于处理空值：当没有匹配记录时返回 0 而非 NULL
        stats = (
            await db.execute(
                select(
                    func.count(LLMCallLog.id),                                   # 总调用次数
                    func.coalesce(func.sum(LLMCallLog.prompt_tokens), 0),        # 输入 Token 总量
                    func.coalesce(func.sum(LLMCallLog.completion_tokens), 0),    # 输出 Token 总量
                    func.coalesce(func.avg(LLMCallLog.latency_ms), 0.0),        # 平均延迟
                    func.count(LLMCallLog.id).filter(LLMCallLog.success == False),  # 失败调用次数
                ).select_from(base.subquery())  # 使用子查询确保日期过滤对所有聚合生效
            )
        ).one()

        total = stats[0]
        err_count = stats[4]
        # 计算错误率：失败次数 / 总次数，保留 4 位小数
        error_rate = round(err_count / total, 4) if total > 0 else 0.0

        # 计算 P95 延迟：取所有调用中延迟最高的前 5% 中的最小值
        # 方法：按延迟降序排列，取前 (total * 5%) 条记录，最后一条即为 P95 分界值
        p95 = 0.0
        if total > 0:
            p95_rows = (
                await db.execute(
                    select(LLMCallLog.latency_ms)
                    .order_by(LLMCallLog.latency_ms.desc())  # 按延迟从高到低排序
                    .limit(max(1, int(total * 0.05)))          # 取前 5% 的记录
                )
            ).all()
            if p95_rows:
                # 前 5% 的最后一条记录值即为 P95（95% 的请求延迟 <= 此值）
                p95 = p95_rows[-1][0]

        # 封装响应数据
        return LLMOverviewOut(
            total_calls=total,
            total_prompt_tokens=stats[1],
            total_completion_tokens=stats[2],
            avg_latency_ms=round(stats[3], 1),   # 平均延迟保留 1 位小数
            p95_latency_ms=round(p95, 1),         # P95 延迟保留 1 位小数
            error_rate=error_rate,
            date_from=date_from,
            date_to=date_to,
        )


@router.get("/trends")
async def llm_trends(
    request: Request,
    days: int = Query(default=7, ge=1, le=90),
) -> list[LLMTrendItem]:
    """获取 LLM 用量按天趋势数据。

    统计最近 N 天内每天的调用次数和 Token 消耗，按日期升序排列。
    适用于前端折线图或柱状图展示用量变化趋势。

    参数：
        request (Request): FastAPI 请求对象（框架自动注入）。
        days (int): 统计天数，范围 1~90，默认最近 7 天。

    返回：
        list[LLMTrendItem]: 每日趋势数据列表，按日期升序排列。
    """
    async with get_db() as db:
        # 计算查询的起始时间：当前 UTC 时间减去指定天数
        since = datetime.now(timezone.utc) - timedelta(days=days)

        # 按天分组聚合：统计每天的总调用次数和 Token 消耗
        rows = (
            await db.execute(
                select(
                    func.date(LLMCallLog.created_at).label("day"),       # 提取日期部分（YYYY-MM-DD）
                    func.count(LLMCallLog.id).label("calls"),            # 当天调用次数
                    func.coalesce(func.sum(LLMCallLog.prompt_tokens), 0),    # 当天输入 Token 总量
                    func.coalesce(func.sum(LLMCallLog.completion_tokens), 0), # 当天输出 Token 总量
                )
                .where(LLMCallLog.created_at >= since)  # 只查询指定天数内的记录
                .group_by("day")                          # 按天分组
                .order_by("day")                          # 按日期升序排列
            )
        ).all()

        # 将查询结果转换为 Pydantic 响应模型列表
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
    """按调用节点分解 LLM 用量。

    将指定时间范围内的 LLM 调用按功能模块（节点）分组统计，
    展示每个节点的调用次数、Token 消耗和平均延迟。
    便于运维人员识别成本热点和性能瓶颈模块。

    参数：
        request (Request): FastAPI 请求对象（框架自动注入）。
        date_from (str | None): 可选，统计起始日期（ISO 格式）。
        date_to (str | None): 可选，统计截止日期（ISO 格式）。

    返回：
        list[LLMNodeBreakdown]: 按调用次数降序排列的各节点用量数据列表。
    """
    async with get_db() as db:
        # 解析日期过滤条件
        df, dt = _parse_date_range(date_from, date_to)

        # 构建按节点分组的聚合查询
        base = select(
            LLMCallLog.node,                                            # 节点名称
            func.count(LLMCallLog.id).label("calls"),                   # 该节点调用次数
            func.coalesce(func.sum(LLMCallLog.prompt_tokens), 0),       # 输入 Token 总量
            func.coalesce(func.sum(LLMCallLog.completion_tokens), 0),   # 输出 Token 总量
            func.coalesce(func.avg(LLMCallLog.latency_ms), 0.0),       # 平均延迟
        ).group_by(LLMCallLog.node)  # 按节点名称分组

        # 应用日期过滤条件
        if df:
            base = base.where(LLMCallLog.created_at >= df)
        if dt:
            base = base.where(LLMCallLog.created_at <= dt)

        # 按调用次数降序排列，用量最高的节点排在最前面
        rows = (await db.execute(base.order_by(func.count(LLMCallLog.id).desc()))).all()

        return [
            LLMNodeBreakdown(
                node=r[0],
                calls=r[1],
                prompt_tokens=r[2],
                completion_tokens=r[3],
                avg_latency_ms=round(r[4], 1),  # 平均延迟保留 1 位小数
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
    """获取 LLM 调用明细列表（分页）。

    分页查询 LLM 调用日志，支持按会话 ID、节点名称、模型名称和日期范围进行筛选。
    结果按调用时间降序排列（最新的在最前）。

    参数：
        request (Request): FastAPI 请求对象（框架自动注入）。
        page (int): 页码，从 1 开始，默认第 1 页。
        page_size (int): 每页记录数，范围 1~100，默认 20 条。
        session_id (str | None): 可选，按会话 ID 筛选。
        node (str | None): 可选，按调用节点名称筛选。
        model (str | None): 可选，按模型名称筛选。
        date_from (str | None): 可选，统计起始日期（ISO 格式）。
        date_to (str | None): 可选，统计截止日期（ISO 格式）。

    返回：
        PaginatedResponse[LLMCallLogItem]: 分页响应，包含当前页数据列表、总记录数和分页信息。
    """
    async with get_db() as db:
        # 解析日期过滤条件
        df, dt = _parse_date_range(date_from, date_to)

        # 构建基础查询，逐步添加筛选条件（动态 WHERE 子句）
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

        # 先统计符合条件的总记录数（用于分页计算）
        count_q = select(func.count()).select_from(base.subquery())
        total = (await db.execute(count_q)).scalar() or 0

        # 计算分页偏移量
        offset = (page - 1) * page_size
        # 按创建时间降序排列，应用分页限制
        rows = (
            await db.execute(
                base.order_by(LLMCallLog.created_at.desc()).offset(offset).limit(page_size)
            )
        ).scalars().all()

        # 将 ORM 查询结果映射为 Pydantic 响应模型列表
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
                created_at=_iso(r.created_at),  # 统一转换为 ISO 格式字符串
            )
            for r in rows
        ]

        return PaginatedResponse(
            items=items, total=total, page=page, page_size=page_size,
        )


# ── 模型配置管理接口 ─────────────────────────────────────────
# 提供模型配置的查询和运行时更新能力，修改后即时生效（热加载）


@router.get("/models")
async def list_model_configs(request: Request) -> list[ModelConfigItem]:
    """获取所有激活的模型配置列表。

    返回当前系统中所有 is_active=True 的模型配置。
    利用内存缓存减少数据库查询，仅在缓存为空时加载。

    参数：
        request (Request): FastAPI 请求对象（框架自动注入）。

    返回：
        list[ModelConfigItem]: 当前所有激活的模型配置条目列表。
    """
    async with get_db() as db:
        cache = await _ensure_cache(db)
        return list(cache.values())


@router.put("/models/{role}")
async def update_model_config(
    role: str,
    body: ModelConfigUpdate,
    request: Request,
) -> ModelConfigItem:
    """更新指定角色（role）的模型配置并热加载缓存。

    只更新请求体中显式提供的字段（部分更新），未提供的字段保持原值不变。
    更新数据库后同步刷新内存缓存，确保后续请求立即使用新配置，无需重启服务。

    参数：
        role (str): 角色名称（路径参数），如 default、consult、explain、dispatch。
        body (ModelConfigUpdate): 请求体，包含需要更新的字段（全部可选）。
        request (Request): FastAPI 请求对象（框架自动注入）。

    返回：
        ModelConfigItem: 更新后的模型配置完整信息。

    异常：
        HTTPException 404: 当指定 role 的激活配置不存在时抛出。
    """
    async with get_db() as db:
        # 查询指定角色对应的激活配置记录
        stmt = select(ModelConfig).where(
            ModelConfig.role == role, ModelConfig.is_active == True
        )
        result = await db.execute(stmt)
        config = result.scalar_one_or_none()

        # 如果不存在该角色配置，返回 404 错误
        if config is None:
            raise HTTPException(status_code=404, detail=f"Model config not found: {role}")

        # 部分更新：只修改用户显式提供的字段，其余保持不变
        if body.model_name is not None:
            config.model_name = body.model_name
        if body.temperature is not None:
            config.temperature = body.temperature
        if body.max_tokens is not None:
            config.max_tokens = body.max_tokens
        if body.description is not None:
            config.description = body.description

        # 记录更新时间
        config.updated_at = datetime.now(timezone.utc)
        # 提交数据库事务
        await db.commit()
        # 刷新 ORM 对象，获取数据库生成的最新值
        await db.refresh(config)

        # 同步更新内存缓存：将 ORM 对象转换为 Pydantic Schema 并写入缓存
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
