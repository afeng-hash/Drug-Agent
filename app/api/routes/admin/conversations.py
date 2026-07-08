"""
Admin 对话管理模块 — 提供会话的查看、搜索、导出功能。

本模块是后台管理系统的核心模块之一，负责所有会话（Session）相关的管理操作：
  1. 会话列表查询（支持多维度筛选 + 分页）
  2. 单个会话详情查看（含完整消息历史和状态快照）
  3. 会话数据导出（支持 JSON / CSV 格式，流式传输避免内存溢出）

核心设计思路：
  - 使用预聚合子查询（subquery）消除 N+1 查询问题，主查询一次 DB 往返获取大部分字段
  - 分页采用 offset/limit 模式，配合 count 子查询获取总数
  - 导出接口使用 StreamingResponse 流式传输，逐行/逐块 yield 数据，避免大会话 OOM
  - 所有时间字段统一通过 _iso() 辅助函数格式化为 ISO 8601 字符串

API 端点：
  GET  /api/v1/admin/conversations              — 分页列表 + 多条件筛选
  GET  /api/v1/admin/conversations/{session_id} — 完整对话详情（消息列表 + 状态快照）
  GET  /api/v1/admin/conversations/{session_id}/export — 导出会话数据（json|csv）
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
    """会话列表项（用于分页列表接口的每一条记录）。"""

    session_id: str
    """会话的业务 ID（UUID 字符串，对外暴露的标识符）"""

    user_id: int | None
    """用户 ID（未登录用户为 None）"""

    user_nickname: str | None
    """用户昵称（未登录用户为 None）"""

    status: str
    """会话状态：active（活跃）、expired（已过期）、closed（已关闭）"""

    message_count: int
    """该会话的总消息数（由预聚合子查询统计）"""

    first_message: str
    """会话的第一条用户消息内容（截取前 100 字符，用于列表预览）"""

    last_message_at: str | None
    """最后一条消息的时间（ISO 8601 格式，无消息时为 None）"""

    intents: list[str]
    """该会话中所有去重后的意图标签列表（从用户消息中聚合）"""

    recommendation_count: int
    """推荐药品的数量（从 state_snapshot 的 recommendations 字段计算）"""

    created_at: str
    """会话创建时间（ISO 8601 格式）"""


class ConversationDetail(BaseModel):
    """会话详情（用于单个会话的完整信息查询接口）。"""

    session_id: str
    """会话的业务 ID（UUID 字符串）"""

    user_id: int | None
    """用户 ID（未登录用户为 None）"""

    user_nickname: str | None
    """用户昵称（未登录用户为 None）"""

    status: str
    """会话状态：active / expired / closed"""

    created_at: str
    """会话创建时间（ISO 8601 格式）"""

    expires_at: str | None
    """会话过期时间（ISO 8601 格式，未设置过期时间则为 None）"""

    updated_at: str | None
    """会话最后更新时间（ISO 8601 格式，从未更新则为 None）"""

    messages: list[dict]
    """消息列表，每条消息包含 role、content、intent、metadata、timestamp 字段"""

    state_snapshot: dict | None
    """会话状态快照（JSON 对象，包含诊断上下文、推荐结果等，无快照则为 None）"""


# ── Helpers ──────────────────────────────────────────────────

def _iso(ts: datetime | None) -> str | None:
    """将 datetime 对象转为 ISO 8601 格式字符串，用于 API 响应的统一时间格式化。

    Args:
        ts: datetime 对象，可能为 None（表示时间字段为空）。

    Returns:
        ISO 8601 格式的时间字符串（如 "2026-07-07T10:30:00"）；若 ts 为 None 则返回 None。
    """
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
    """分页查询会话列表，支持多维度筛选。

    这一步的作用：
      为后台管理系统提供会话数据的分页查询能力。支持按会话状态、创建日期范围、
      用户 ID、消息关键词等维度进行筛选，返回带分页信息的会话列表。

    核心优化：
      使用预聚合子查询（subquery）将消息统计和意图聚合提前计算，再与主查询进行
      OUTER JOIN，使得主查询只需一次 DB 往返即可获取大部分字段，避免 N+1 问题。

    Args:
        request: FastAPI Request 对象（用于依赖注入和请求上下文）。
        page: 页码，从 1 开始，默认第 1 页（ge=1 保证页码 >= 1）。
        page_size: 每页条数，默认 20 条，范围 1-100（le=100 防止一次查询过多数据）。
        status: 按会话状态筛选（active 活跃 / expired 已过期 / closed 已关闭），不传则不筛选。
        date_from: 按创建时间筛选的起始日期（ISO 8601 格式，如 "2026-07-01"）。
        date_to: 按创建时间筛选的结束日期（ISO 8601 格式，如 "2026-07-07"）。
        user_id: 按用户 ID 筛选，不传则不筛选。
        keyword: 按消息内容模糊搜索（ILIKE 不区分大小写），在用户消息中匹配。

    Returns:
        PaginatedResponse[ConversationListItem]:
            - items: 当前页的会话列表项
            - total: 符合条件的总记录数（用于前端分页组件计算总页数）
            - page: 当前页码
            - page_size: 每页条数
    """
    async with get_db() as db:
        # ══════════════════════════════════════════════════════════
        # 阶段一：预聚合子查询（消除 N+1 问题）
        # 将所有需要聚合计算的字段提前在数据库层面完成，
        # 避免在主查询的每一行上再发起额外查询。
        # ══════════════════════════════════════════════════════════

        # 子查询 1 —— 每个 session 的消息统计
        # 按 session_id 分组，统计 count(id) 得到消息数，max(created_at) 得到最后消息时间
        msg_stats = (
            select(
                Message.session_id,
                func.count(Message.id).label("msg_count"),
                func.max(Message.created_at).label("last_msg_at"),
            )
            .group_by(Message.session_id)
            .subquery()
        )

        # 子查询 2 —— 每个 session 的意图（intents）聚合
        # 从 role='user' 的消息中提取 intent，distinct 去重后 array_agg 聚合成数组
        # array_remove(..., None) 过滤掉 intent 为 NULL 的条目
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

        # ══════════════════════════════════════════════════════════
        # 阶段二：主查询（一次 DB 往返获取大部分字段）
        # 将 Session 表与预聚合子查询通过 OUTER JOIN 关联，
        # 同时 JOIN User 表获取昵称，使用 coalesce 处理 NULL 默认值。
        # ══════════════════════════════════════════════════════════
        base = (
            select(
                SessionModel.id,
                SessionModel.session_id,
                SessionModel.user_id,
                SessionModel.status,
                SessionModel.created_at,
                SessionModel.state_snapshot,
                User.nickname.label("user_nickname"),
                # coalesce: 若子查询无匹配行（session 无消息），消息数默认为 0
                func.coalesce(msg_stats.c.msg_count, 0).label("message_count"),
                # coalesce: 若无最后消息，默认取 session 创建时间作为排序/展示用
                func.coalesce(msg_stats.c.last_msg_at, SessionModel.created_at).label(
                    "last_message_at"
                ),
                # coalesce: 若无意向数据，默认返回空数组
                func.coalesce(intent_agg.c.intents, []).label("intents"),
            )
            # LEFT OUTER JOIN User 表 —— 获取用户昵称（未登录用户的 user_id 为 NULL，JOIN 不上）
            .outerjoin(User, SessionModel.user_id == User.id)
            # LEFT OUTER JOIN 消息统计子查询
            .outerjoin(
                msg_stats, SessionModel.id == msg_stats.c.session_id,
            )
            # LEFT OUTER JOIN 意图聚合子查询
            .outerjoin(
                intent_agg, SessionModel.id == intent_agg.c.session_id,
            )
        )

        # ══════════════════════════════════════════════════════════
        # 阶段三：筛选条件（动态拼接 WHERE 子句）
        # 根据前端传入的查询参数，有条件地添加过滤条件。
        # ══════════════════════════════════════════════════════════
        if status:
            # 按会话状态精确匹配
            base = base.where(SessionModel.status == status)
        if user_id is not None:
            # 按用户 ID 精确匹配
            base = base.where(SessionModel.user_id == user_id)
        if date_from:
            # 按创建时间起始筛选（>= date_from）
            base = base.where(SessionModel.created_at >= datetime.fromisoformat(date_from))
        if date_to:
            # 按创建时间结束筛选（<= date_to）
            base = base.where(SessionModel.created_at <= datetime.fromisoformat(date_to))
        if keyword:
            # 按消息内容模糊搜索：先子查询找出包含关键词的 session_id，
            # 再用 IN 条件过滤主查询，ILIKE 不区分大小写
            base = base.where(
                SessionModel.id.in_(
                    select(Message.session_id).where(
                        Message.content.ilike(f"%{keyword}%")
                    )
                )
            )

        # ══════════════════════════════════════════════════════════
        # 阶段四：排序 + 总数统计
        # ══════════════════════════════════════════════════════════
        # 按创建时间倒序排列（最新的会话在前）
        base = base.order_by(SessionModel.created_at.desc())

        # 子查询统计总数 —— 将 base 查询包装为子查询后再 count，
        # 这样可以拿到筛选后的总记录数（不受分页影响）
        count_q = select(func.count()).select_from(base.subquery())
        total = (await db.execute(count_q)).scalar() or 0

        # ══════════════════════════════════════════════════════════
        # 阶段五：分页（offset + limit）
        # ══════════════════════════════════════════════════════════
        offset = (page - 1) * page_size
        rows = (await db.execute(base.offset(offset).limit(page_size))).all()

        # ══════════════════════════════════════════════════════════
        # 阶段六：补充每行的 first_message 和 recommendation_count
        # 这两个字段不适合在主查询的聚合中计算（first_message 需要排序取第一条，
        # recommendation_count 需要解析 JSON 字段），因此在行循环中单独获取。
        # 每行额外 1 次标量子查询（first_message），有索引支持，性能可接受。
        # ══════════════════════════════════════════════════════════
        items = []
        for row in rows:
            # 将 SQLAlchemy Row 对象转为字典，方便按列名取值
            d = row._asdict()
            sid = d["id"]  # sessions.id (int FK)，用于关联消息表

            # 标量子查询 —— 获取该 session 的第一条用户消息内容
            # 按 created_at 升序取第一条（最早的消息），limit(1) 保证只返回一行
            first_msg = (
                await db.execute(
                    select(Message.content)
                    .where(Message.session_id == sid)
                    .where(Message.role == "user")
                    .order_by(Message.created_at.asc())
                    .limit(1)
                )
            ).scalar()

            # 从 state_snapshot JSON 字段中计算推荐药品数量
            # state_snapshot 可能为 None / 非 dict / 无 recommendations 字段，逐一防御
            snap = d.get("state_snapshot")
            rec_count = 0
            if isinstance(snap, dict):
                recs = snap.get("recommendations", [])
                rec_count = len(recs) if isinstance(recs, list) else 0

            # 组装响应项
            items.append(ConversationListItem(
                session_id=d["session_id"],
                user_id=d["user_id"],
                user_nickname=d["user_nickname"],
                status=d["status"],
                message_count=d["message_count"],
                # first_message 截取前 100 字符，避免列表项内容过长；无消息时为空字符串
                first_message=(first_msg or "")[:100],
                last_message_at=_iso(d["last_message_at"]),
                intents=list(d["intents"] or []),
                recommendation_count=rec_count,
                created_at=_iso(d["created_at"]),
            ))

        # 返回分页响应对象
        return PaginatedResponse(
            items=items, total=total, page=page, page_size=page_size,
        )


@router.get("/{session_id}")
async def get_conversation(
    session_id: str,
    request: Request,
) -> ConversationDetail:
    """获取单个会话的完整详情，包括消息历史和状态快照。

    这一步的作用：
      为后台管理员提供查看单个会话完整上下文的能力。通过 selectinload
      预加载关联的消息列表，一次性获取会话信息 + 所有消息 + 用户昵称，
      组装为结构化的 ConversationDetail 响应。

    与列表接口的区别：
      列表接口只返回摘要信息（消息数、首条消息等），本接口返回完整的消息
      列表和 state_snapshot，用于管理员深入分析单个会话。

    Args:
        session_id: 会话的业务 ID（UUID 字符串，来自 URL 路径参数）。
        request: FastAPI Request 对象（用于依赖注入和请求上下文）。

    Returns:
        ConversationDetail: 包含完整会话数据的响应对象。
            - session_id / user_id / user_nickname / status: 基础会话信息
            - created_at / expires_at / updated_at: 时间信息（ISO 8601 格式）
            - messages: 完整的消息列表，每条含 role、content、intent、metadata、timestamp
            - state_snapshot: 会话状态快照（诊断上下文、推荐结果等原始 JSON）

    Raises:
        HTTPException(404): 当指定的 session_id 在数据库中不存在时抛出。
    """
    async with get_db() as db:
        from sqlalchemy.orm import selectinload

        # 构建查询 —— 使用 selectinload 预加载 messages 关系，
        # 避免后续访问 session.messages 时触发额外的懒加载查询
        stmt = (
            select(SessionModel)
            .options(selectinload(SessionModel.messages))
            .where(SessionModel.session_id == session_id)
        )
        result = await db.execute(stmt)
        session = result.scalar_one_or_none()

        # 会话不存在则返回 404
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")

        # 单独查询用户昵称（通过 user_id 外键关联 User 表）
        user_nickname = None
        if session.user_id:
            user = await db.get(User, session.user_id)
            user_nickname = user.nickname if user else None

        # 遍历消息列表，将每条消息转为字典格式
        # metadata_ 是 SQLAlchemy 模型字段名（metadata 是 Python 保留字，模型加了后缀下划线）
        messages = []
        for m in (session.messages or []):
            messages.append({
                "role": m.role,          # 消息角色：user / assistant / system
                "content": m.content,    # 消息正文内容
                "intent": m.intent,      # 意图标签（仅 user 消息有值）
                "metadata": m.metadata_, # 消息元数据（JSON 格式的附加信息）
                "timestamp": _iso(m.created_at),  # 消息时间（ISO 8601）
            })

        # 组装并返回详情响应
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
    """导出单个会话的完整数据，支持 JSON 和 CSV 两种格式。

    这一步的作用：
      为后台管理员提供将会话数据导出为文件的功能。使用 StreamingResponse
      实现流式传输，逐行/逐块 yield 数据，避免在内存中一次性构建整个文件内容，
      从而防止大会话（消息数很多）时出现 OOM（内存溢出）问题。

    性能考量：
      - CSV 导出：逐行写入，每条消息独立一行，内存占用为单条消息大小
      - JSON 导出：逐块构建 JSON 结构，最后才闭合大括号，避免一次性 json.dumps 全部数据
      - 两种格式均使用异步生成器（async generator），支持背压（backpressure）

    Args:
        session_id: 会话的业务 ID（UUID 字符串，来自 URL 路径参数）。
        format: 导出格式，可选 "json" 或 "csv"，默认 "json"。

    Returns:
        StreamingResponse:
            - 流式文件下载响应
            - 响应头 Content-Disposition 指定文件名，浏览器会自动触发下载
            - media_type 根据格式设定：text/csv 或 application/json

    Raises:
        HTTPException(404): 当指定的 session_id 在数据库中不存在时抛出。
    """
    async with get_db() as db:
        from sqlalchemy.orm import selectinload

        # 构建查询 —— 使用 selectinload 预加载关联的 messages，
        # 一次性获取 session + 所有消息，避免后续懒加载
        stmt = (
            select(SessionModel)
            .options(selectinload(SessionModel.messages))
            .where(SessionModel.session_id == session_id)
        )
        result = await db.execute(stmt)
        session = result.scalar_one_or_none()

        # 会话不存在则返回 404
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")

        # ══════════════════════════════════════════════════════════
        # CSV 格式导出
        # ══════════════════════════════════════════════════════════
        if format == "csv":
            # 构造下载文件名
            filename = f"conversation_{session_id}.csv"

            async def csv_generator():
                """CSV 异步生成器 —— 逐行 yield CSV 内容，避免一次性加载全部数据到内存。"""
                # 写入 CSV 表头
                yield "role,content,intent,timestamp\n"
                for m in (session.messages or []):
                    # CSV 转义规则：字段内容中的双引号需转义为两个双引号（""），
                    # 含逗号或双引号的字段需要用引号包裹
                    content_escaped = m.content.replace('"', '""')
                    row = f'{m.role},"{content_escaped}",{m.intent or ""},{_iso(m.created_at) or ""}\n'
                    yield row

            # 使用 StreamingResponse 返回流式 CSV 文件
            return StreamingResponse(
                csv_generator(),
                media_type="text/csv",
                headers={"Content-Disposition": f"attachment; filename={filename}"},
            )
        # ══════════════════════════════════════════════════════════
        # JSON 格式导出（默认格式）
        # ══════════════════════════════════════════════════════════
        else:
            # 构造下载文件名
            filename = f"conversation_{session_id}.json"

            async def json_generator():
                """JSON 异步生成器 —— 逐块构建 JSON 结构，避免一次性序列化所有消息到大字符串。"""
                # 手动拼接 JSON，不使用 json.dumps(all_data)，以控制内存峰值
                yield '{\n'
                yield f'  "session_id": "{session_id}",\n'
                yield f'  "status": "{session.status}",\n'
                yield '  "messages": [\n'
                msgs = session.messages or []
                for i, m in enumerate(msgs):
                    # 判断是否需要逗号分隔（最后一条消息不加逗号，保证合法 JSON）
                    comma = "," if i < len(msgs) - 1 else ""
                    # 逐条序列化消息（ensure_ascii=False 保留中文原文）
                    msg_json = json.dumps({
                        "role": m.role,
                        "content": m.content,
                        "intent": m.intent,
                        "timestamp": _iso(m.created_at),
                    }, ensure_ascii=False)
                    yield f"    {msg_json}{comma}\n"
                yield '  ],\n'
                # 序列化 state_snapshot（含缩进美化，null 时不序列化直接输出 "null"）
                snapshot_json = json.dumps(
                    session.state_snapshot, ensure_ascii=False, indent=2
                ) if session.state_snapshot else "null"
                yield f'  "state_snapshot": {snapshot_json}\n'
                # 闭合最外层大括号，JSON 文档完整
                yield '}\n'

            # 使用 StreamingResponse 返回流式 JSON 文件
            return StreamingResponse(
                json_generator(),
                media_type="application/json",
                headers={"Content-Disposition": f"attachment; filename={filename}"},
            )
