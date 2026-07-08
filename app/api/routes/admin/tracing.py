"""
Admin Agent Trace — 会话级链路追踪 + Turn 级详情。

本模块提供后台管理端的链路追踪功能，用于监控和调试 Agent 对话流程的完整执行链路。
主要功能包括：
  1. 分页查询所有 Trace 会话列表（按 session 维度聚合）
  2. 查看单个 turn 的完整执行链路（节点时间线 + LLM 调用 + 关联消息）
  3. 获取全局链路追踪聚合统计（总 turn 数、错误率、平均耗时）

API 端点：
  GET  /api/v1/admin/traces              — Trace 会话列表（分页）
  GET  /api/v1/admin/traces/{turn_id}    — 单 turn 完整链路详情
  GET  /api/v1/admin/traces/stats        — 聚合统计（含错误率、平均耗时）
"""

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.routes.admin.schemas import PaginatedResponse
from app.db.database import get_db
from app.db.models import LLMCallLog, Message, Session as SessionModel, TraceLog

# 创建路由实例，统一前缀 /traces，归入 admin 标签组
router = APIRouter(prefix="/traces", tags=["admin"])


def _iso(ts: datetime | None) -> str | None:
    """将 datetime 对象转换为 ISO 8601 格式字符串。

    参数:
        ts: 待转换的 datetime 对象，可为 None。

    返回:
        ISO 8601 格式的时间字符串；若 ts 为 None 则返回 None。
    """
    return ts.isoformat() if ts else None


def _strip_uuid_suffix(turn_id: str) -> str:
    """剥离 turn_id 末尾的 uuid8 后缀（新格式 → 旧格式兼容）。

    背景: turn_id 存在新旧两种格式——
      新格式: "{session_id}:{count}:{uuid8}"  → 剥离后: "{session_id}:{count}"
      旧格式: "{session_id}:{count}"          → 不变

    作用: 用于向后兼容查询。当用新格式 turn_id 精确匹配找不到历史数据时，
          调用此函数剥离末尾 uuid8 后缀，再用剥离后的前缀进行模糊（LIKE）查询，
          确保能够查到旧格式存储的历史记录。

    参数:
        turn_id: 待处理的 turn_id 字符串，可能是新格式或旧格式。

    返回:
        剥离 uuid8 后缀后的 turn_id 前缀；若已是旧格式则原样返回。
    """
    # 从右侧按 ":" 分割一次，尝试识别末尾是否有 8 位 hex uuid
    parts = turn_id.rsplit(":", 1)
    if len(parts) == 3 and len(parts[-1]) == 8:
        # 最后一段是 8 位 hex → 确认为新格式，剥离 uuid8 后缀
        return f"{parts[0]}:{parts[1]}"
    # 旧格式或无法识别，保持原样返回
    return turn_id


# ── 数据模型定义（响应 Schema） ──────────────────────────────────


class TraceSessionItem(BaseModel):
    """Trace 会话列表中的单个条目。

    每个条目对应一个 session，聚合了该 session 下所有 turn 的概览信息。
    """
    session_id: str                             # 会话唯一标识
    turn_count: int                             # 该会话下的 turn 总数
    total_duration_ms: float | None             # 所有 turn 累计耗时（毫秒），无数据时为 None
    error_count: int                            # 错误节点总数
    first_node: str | None                      # 该会话第一个执行的节点名称
    last_node: str | None                       # 该会话最后一个执行的节点名称
    created_at: str | None                      # 会话创建时间（ISO 8601 格式）


class NodeTraceItem(BaseModel):
    """单个节点的链路追踪记录。

    记录一个执行节点的状态、耗时及错误信息。
    """
    node: str                                   # 节点名称（如 Dispatcher、ReactAgent 等）
    status: str                                 # 执行状态（如 "completed"、"error"）
    duration_ms: float | None                   # 节点执行耗时（毫秒），无数据时为 None
    metadata: dict | None                       # 节点附加元数据（如配置、参数等）
    error_message: str | None                   # 错误信息，成功时为空
    started_at: str | None                      # 节点开始执行时间（ISO 8601 格式）


class TurnTraceDetail(BaseModel):
    """单个 turn 的完整链路详情。

    包含该 turn 下所有节点的执行时间线、LLM 调用记录和关联的消息。
    """
    turn_id: str                                # turn 的唯一标识
    session_id: str                             # 所属会话标识
    nodes: list[NodeTraceItem]                  # 节点执行时间线（按开始时间升序）
    llm_calls: list[dict]                       # LLM 调用详情列表（模型、token、延迟等）
    messages: list[dict]                        # 关联的消息记录（角色、内容摘要等）


class TraceStats(BaseModel):
    """链路追踪全局聚合统计。

    用于运维监控仪表盘，展示系统整体健康状况。
    """
    total_turns: int                            # 总 turn 数
    error_rate: float                           # 错误率（错误数 / 总数，保留 4 位小数）
    avg_duration_ms: float | None               # 平均每个节点耗时（毫秒），无数据时为 None


# ── API 路由端点 ──────────────────────────────────────────────────


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

    作用: 按 session_id 维度聚合所有 turn 的 trace 记录，返回每个 session 的概览信息，
          包括 turn 总数、总耗时、错误数、首尾节点等，支持分页、时间范围和关键词过滤。

    参数:
        request: FastAPI Request 对象（框架注入，用于上下文传递）。
        page: 页码，从 1 开始，默认第 1 页。
        page_size: 每页条数，范围 1~100，默认 20。
        date_from: 起始日期过滤（ISO 8601 格式），筛选创建时间 >= 该日期的记录。
        date_to: 截止日期过滤（ISO 8601 格式），筛选创建时间 <= 该日期的记录。
        status: 状态过滤，可选 "completed" 或 "error"。
        keyword: 关键词搜索，在消息内容中模糊匹配。

    返回:
        PaginatedResponse[TraceSessionItem]: 分页响应，包含 items、total、page、page_size。
    """
    async with get_db() as db:
        # 第一步：按 session_id 聚合所有 trace 记录，计算各 session 的概览指标
        agg = (
            select(
                TraceLog.session_id,
                func.count(func.distinct(TraceLog.turn_id)).label("turn_count"),  # 去重统计 turn 数
                func.coalesce(func.sum(TraceLog.duration_ms), 0.0).label("total_duration"),  # 累计耗时，NULL 按 0 处理
                func.count(TraceLog.id).filter(TraceLog.status == "error").label("error_count"),  # 仅统计状态为 error 的记录
                func.min(TraceLog.started_at).label("first_at"),  # 最早 trace 时间，即该 session 的创建时间
            )
            .group_by(TraceLog.session_id)
            .subquery()  # 将聚合结果作为子查询，便于后续分页和筛选
        )

        # 第二步：构建基础查询
        base = select(
            agg.c.session_id,
            agg.c.turn_count,
            agg.c.total_duration,
            agg.c.error_count,
            agg.c.first_at,
        )

        # 第三步：应用时间范围筛选
        if date_from:
            base = base.where(agg.c.first_at >= datetime.fromisoformat(date_from))
        if date_to:
            base = base.where(agg.c.first_at <= datetime.fromisoformat(date_to))

        # 第四步：应用关键词搜索（在 Message 表中模糊匹配内容）
        if keyword:
            base = base.where(
                agg.c.session_id.in_(
                    select(Message.session_id).where(
                        Message.content.ilike(f"%{keyword}%")  # 不区分大小写的模糊匹配
                    )
                )
            )

        # 第五步：统计符合条件的总记录数（用于前端分页）
        count_q = select(func.count()).select_from(base.subquery())
        total = (await db.execute(count_q)).scalar() or 0

        # 第六步：计算分页偏移量，按创建时间倒序排列
        offset = (page - 1) * page_size
        base = base.order_by(agg.c.first_at.desc())
        rows = (await db.execute(base.offset(offset).limit(page_size))).all()

        # 第七步：遍历每条记录，组装响应数据
        items = []
        for row in rows:
            d = row._asdict()  # 将行结果转为字典，方便字段访问
            session_id = d["session_id"]
            total_dur = float(d["total_duration"]) if d["total_duration"] else None

            # 查询该 session 的第一个节点（按 started_at 升序取第一条）
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

            # 查询该 session 的最后一个节点（按 started_at 降序取第一条）
            last_node_row = (
                await db.execute(
                    select(TraceLog.node)
                    .where(TraceLog.session_id == session_id)
                    .order_by(TraceLog.started_at.desc())
                    .limit(1)
                )
            ).first()
            last_node = last_node_row[0] if last_node_row else None

            # 组装单条结果
            items.append(
                TraceSessionItem(
                    session_id=session_id,
                    turn_count=d["turn_count"],
                    total_duration_ms=round(total_dur, 1) if total_dur else None,  # 保留 1 位小数
                    error_count=d["error_count"],
                    first_node=first_node,
                    last_node=last_node,
                    created_at=_iso(d["first_at"]),
                )
            )

        # 返回标准分页响应
        return PaginatedResponse(
            items=items, total=total, page=page, page_size=page_size,
        )


@router.get("/{turn_id}")
async def get_turn_trace(turn_id: str, request: Request) -> TurnTraceDetail:
    """获取单个 turn 的完整链路详情。

    作用: 根据 turn_id 查询该轮对话的完整执行链路，包括：
          1. 节点执行时间线（按开始时间升序排列）
          2. LLM 调用详情（模型、token 消耗、延迟等）
          3. 关联的消息记录（用户/助手对话内容摘要）
          同时兼容新旧两种 turn_id 格式，确保历史数据可查。

    参数:
        turn_id: 待查询的 turn 唯一标识（支持新旧两种格式）。
        request: FastAPI Request 对象（框架注入）。

    返回:
        TurnTraceDetail: 包含 nodes、llm_calls、messages 三部分完整链路数据。

    异常:
        HTTPException(404): 当 turn_id 对应的链路数据不存在时抛出。
    """
    async with get_db() as db:
        # 第一步：剥离新版 turn_id 的 uuid8 后缀，实现新旧格式兼容
        base_turn = _strip_uuid_suffix(turn_id)
        turn_filters = [TraceLog.turn_id == turn_id]
        if base_turn != turn_id:
            # 新旧格式不一致时，同时用两种格式查询，扩大匹配范围
            turn_filters.append(TraceLog.turn_id == base_turn)

        from sqlalchemy import or_

        # 第二步：查询节点执行时间线，按开始时间升序
        node_rows = (
            await db.execute(
                select(TraceLog)
                .where(or_(*turn_filters))  # 用 OR 条件同时匹配新旧格式
                .order_by(TraceLog.started_at.asc())
            )
        ).scalars().all()

        # 若无记录则返回 404
        if not node_rows:
            raise HTTPException(status_code=404, detail="Turn not found")

        # 从第一条记录中提取 session_id，后续查询需要
        session_id = node_rows[0].session_id

        # 第三步：将 TraceLog 行记录转换为 NodeTraceItem 响应对象
        nodes = [
            NodeTraceItem(
                node=n.node,
                status=n.status,
                duration_ms=round(n.duration_ms, 1) if n.duration_ms else None,  # 保留1位小数
                metadata=n.metadata_,
                error_message=n.error_message,
                started_at=_iso(n.started_at),
            )
            for n in node_rows
        ]

        # 第四步：查询关联的 LLM 调用记录（同样兼容新旧格式）
        llm_filters = [LLMCallLog.turn_id == turn_id]
        if base_turn != turn_id:
            llm_filters.append(LLMCallLog.turn_id == base_turn)

        llm_rows = (
            await db.execute(
                select(LLMCallLog)
                .where(or_(*llm_filters))
                .order_by(LLMCallLog.created_at.asc())  # 按调用时间升序
            )
        ).scalars().all()

        # 第五步：兜底策略 —— 若 turn 级查询无结果，降级为 session 级查询
        # 这样即使某些 LLM 调用未关联到具体 turn，也能通过 session 关联查到
        if not llm_rows:
            llm_rows = (
                await db.execute(
                    select(LLMCallLog)
                    .where(LLMCallLog.session_id == session_id)
                    .order_by(LLMCallLog.created_at.asc())
                )
            ).scalars().all()

        # 第六步：将 LLM 调用记录转为字典列表（提取关键字段）
        llm_calls = [
            {
                "id": c.id,
                "node": c.node,                       # 发起调用的节点名称
                "model": c.model,                     # 使用的模型名称
                "prompt_tokens": c.prompt_tokens,     # 提示词 token 消耗
                "completion_tokens": c.completion_tokens,  # 补全 token 消耗
                "latency_ms": c.latency_ms,           # 调用延迟（毫秒）
                "success": c.success,                 # 调用是否成功
                "error_message": c.error_message,     # 错误信息（成功时为空）
                "created_at": _iso(c.created_at),
            }
            for c in llm_rows
        ]

        # 第七步：查询关联的对话消息（通过 session_id 查 messages 表）
        from sqlalchemy import and_

        # 先查 Session 表获取内部 id（外键关联用）
        sess_row = (
            await db.execute(
                select(SessionModel.id).where(SessionModel.session_id == session_id)
            )
        ).scalar_one_or_none()

        msg_rows = []
        if sess_row:
            # 通过 session 内部 id 查询该会话的所有消息，按创建时间升序
            msg_rows = (
                await db.execute(
                    select(Message)
                    .where(Message.session_id == sess_row)
                    .order_by(Message.created_at.asc())
                )
            ).scalars().all()

        # 第八步：将消息记录转为字典列表（截断长内容避免响应过大）
        messages = [
            {
                "role": m.role,                    # 角色：user / assistant / system
                "content": m.content[:500],        # 截断至 500 字符，避免超长内容撑大响应
                "intent": m.intent,                # 意图分类标签
                "timestamp": _iso(m.created_at),
            }
            for m in msg_rows
        ]

        # 返回完整链路详情
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
    """链路追踪聚合统计。

    作用: 提供全局运维监控维度的聚合指标，包括总 turn 数、错误率和平均耗时，
          支持按时间范围过滤，可用于仪表盘展示、告警和性能分析。

    参数:
        request: FastAPI Request 对象（框架注入）。
        date_from: 起始日期过滤（ISO 8601 格式），统计 started_at >= 该日期的记录。
        date_to: 截止日期过滤（ISO 8601 格式），统计 started_at <= 该日期的记录。

    返回:
        TraceStats: 包含 total_turns、error_rate、avg_duration_ms 三项聚合指标。
                    若 total_turns 为 0，error_rate 返回 0.0 避免除零错误。
    """
    async with get_db() as db:
        # 构建聚合查询：统计去重 turn 总数、错误数和平均耗时
        base = select(
            func.count(func.distinct(TraceLog.turn_id)),  # 去重统计 turn 总数
            func.count(TraceLog.id).filter(TraceLog.status == "error"),  # 仅统计 error 状态的节点数
            func.avg(TraceLog.duration_ms),  # 所有节点的平均耗时
        )

        # 应用时间范围过滤
        if date_from:
            base = base.where(TraceLog.started_at >= datetime.fromisoformat(date_from))
        if date_to:
            base = base.where(TraceLog.started_at <= datetime.fromisoformat(date_to))

        # 执行查询
        row = (await db.execute(base)).one()

        total = row[0] or 0
        errors = row[1] or 0

        # 计算错误率（避免除零）
        return TraceStats(
            total_turns=total,
            error_rate=round(errors / total, 4) if total > 0 else 0.0,  # 保留 4 位小数
            avg_duration_ms=round(row[2], 1) if row[2] else None,         # 保留 1 位小数
        )
