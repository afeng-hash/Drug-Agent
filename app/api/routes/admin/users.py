"""
Admin 用户管理模块 — 提供后台管理员查看用户及其会话历史的功能。

本模块是后台管理系统的用户管理子模块，提供了三个核心接口：
- GET  /api/v1/admin/users              — 分页列表 + 模糊搜索
- GET  /api/v1/admin/users/{id}         — 用户详情（含健康画像和最近会话）
- GET  /api/v1/admin/users/{id}/sessions — 某用户的全部会话列表（分页）

主要用途：
  1. 管理员可以浏览所有注册用户的基本信息（外部ID、昵称、活跃时间等）
  2. 支持按 external_id 或 nickname 进行模糊搜索，方便快速定位用户
  3. 可以查看单个用户的详细信息，包括健康画像（health_profile）和最近10条会话
  4. 可以分页查看某个用户的完整会话历史，每条会话附带消息数量统计

依赖说明：
  - 使用 SQLAlchemy 异步查询数据库（User、Session、Message 三张表）
  - 通过 FastAPI 的 APIRouter 注册路由，前缀为 /users
  - 返回值统一使用 PaginatedResponse 进行分页封装
"""

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.routes.admin.schemas import PaginatedResponse
from app.db.database import get_db
from app.db.models import Message, Session as SessionModel, User

router = APIRouter(prefix="/users", tags=["admin"])


def _iso(ts: datetime | None) -> str | None:
    """
    将 datetime 对象转换为 ISO 8601 格式字符串，用于 JSON 序列化响应。

    参数:
        ts (datetime | None): 待转换的时间戳对象，可能为 None（表示用户从未活跃或记录缺失）。

    返回值:
        str | None: 如果 ts 不为 None，返回 ISO 8601 格式的时间字符串（如 "2025-06-15T14:30:00"）；
                    如果 ts 为 None，返回 None，表示该时间字段无有效值。

    用途:
        这是一个模块级私有辅助函数，用于统一处理所有 API 响应中的时间字段序列化，
        避免在多个接口中重复编写 isoformat() 调用和 None 检查逻辑。
    """
    # 若时间戳存在则转为 ISO 格式字符串，否则返回 None（表示无记录）
    return ts.isoformat() if ts else None


# ── Schema: API 响应数据结构定义 ──────────────────────────────────


class UserListItem(BaseModel):
    """
    用户列表项 Schema —— 用于 list_users 接口的分页列表中的每一项。

    与 UserDetail 相比，此结构更精简，不包含 health_profile 和 recent_sessions，
    适合在列表页快速展示用户摘要信息。
    """
    id: int                     # 用户主键 ID（数据库自增）
    external_id: str            # 外部标识符（如第三方登录的唯一 ID）
    nickname: str | None        # 用户昵称，可能为空（用户未设置时）
    session_count: int          # 该用户创建的会话总数
    last_active_at: str | None  # 最近一次活跃时间（ISO 8601 字符串），从未活跃则为 None
    created_at: str | None      # 用户注册/创建时间（ISO 8601 字符串），历史数据可能为 None


class UserDetail(BaseModel):
    """
    用户详情 Schema —— 用于 get_user 接口返回单个用户的完整信息。

    除了列表字段外，还包含：
    - health_profile: 用户的健康画像数据（JSON 字典，存储症状、疾病、过敏等信息）
    - recent_sessions: 用户最近 10 条会话的简要信息（session_id、状态、创建时间）
    """
    id: int                     # 用户主键 ID（数据库自增）
    external_id: str            # 外部标识符（如第三方登录的唯一 ID）
    nickname: str | None        # 用户昵称，可能为空
    health_profile: dict        # 健康画像数据（JSON 字典），若用户未填写则为空字典 {}
    session_count: int          # 该用户创建的会话总数
    last_active_at: str | None  # 最近一次活跃时间（ISO 8601 字符串），从未活跃则为 None
    created_at: str | None      # 用户注册/创建时间（ISO 8601 字符串），历史数据可能为 None
    recent_sessions: list[dict] # 最近 10 条会话列表，每条包含 session_id、status、created_at


# ── Routes ──────────────────────────────────────────────────


@router.get("")
async def list_users(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    search: str | None = Query(default=None, description="搜索 external_id 或 nickname"),
) -> PaginatedResponse[UserListItem]:
    """
    分页查询用户列表，支持模糊搜索。

    这一步的作用:
        为后台管理页面提供用户列表数据。管理员可以按页码浏览所有注册用户，
        并通过 search 参数按 external_id 或 nickname 进行模糊搜索。
        每个用户项会额外统计其会话总数（session_count），方便管理员评估用户活跃度。

    参数:
        request (Request): FastAPI 的请求对象，用于获取请求上下文（如数据库连接等）。
        page (int): 页码，从 1 开始，最小值为 1。默认第 1 页。
        page_size (int): 每页显示的记录数，范围 1~100。默认每页 20 条。
        search (str | None): 模糊搜索关键字，同时匹配 external_id 和 nickname 字段（ILIKE 不区分大小写）。
                             为 None 或空字符串时不进行过滤。

    返回值:
        PaginatedResponse[UserListItem]: 分页响应对象，包含:
            - items: 当前页的用户列表（UserListItem 数组）
            - total: 符合条件的用户总数
            - page: 当前页码
            - page_size: 每页记录数
    """
    # 获取数据库异步会话
    async with get_db() as db:
        # 构建基础查询：选取用户的核心字段
        base = select(
            User.id,
            User.external_id,
            User.nickname,
            User.last_active_at,
            User.created_at,
        )

        # 如果传入了搜索关键字，用 ILIKE 对 external_id 和 nickname 做模糊匹配（不区分大小写）
        if search:
            base = base.where(
                User.external_id.ilike(f"%{search}%")
                | User.nickname.ilike(f"%{search}%")
            )

        # 统计符合条件的总记录数（用于前端分页组件显示总页数）
        # 将 base 查询作为子查询，再用 count(*) 统计行数
        count_q = select(func.count()).select_from(base.subquery())
        total = (await db.execute(count_q)).scalar() or 0

        # 计算当前页的偏移量，按 last_active_at 降序排列（最近活跃的用户排在前面）
        offset = (page - 1) * page_size
        base = base.order_by(User.last_active_at.desc())
        # 执行分页查询：跳过 offset 条，取 page_size 条
        rows = (await db.execute(base.offset(offset).limit(page_size))).all()

        # 遍历每一行结果，组装 UserListItem 响应对象
        items = []
        for row in rows:
            # 将 SQLAlchemy Row 对象转为字典，方便按字段名取值
            d = row._asdict()
            uid = d["id"]

            # 查询该用户的会话总数（用于展示用户活跃度）
            sess_count = (
                await db.execute(
                    select(func.count(SessionModel.id)).where(
                        SessionModel.user_id == uid
                    )
                )
            ).scalar() or 0

            # 构建列表项，时间字段通过 _iso() 统一转为 ISO 格式字符串
            items.append(
                UserListItem(
                    id=uid,
                    external_id=d["external_id"],
                    nickname=d["nickname"],
                    session_count=sess_count,
                    last_active_at=_iso(d["last_active_at"]),
                    created_at=_iso(d["created_at"]),
                )
            )

        # 返回分页响应
        return PaginatedResponse(
            items=items, total=total, page=page, page_size=page_size,
        )


@router.get("/{user_id}")
async def get_user(user_id: int, request: Request) -> UserDetail:
    """
    获取单个用户的详细信息，包含健康画像和最近会话。

    这一步的作用:
        管理员点击用户列表中的某个用户后，调用此接口查看该用户的完整档案。
        返回数据包括用户基本信息、健康画像（health_profile，存储症状/疾病/过敏等）、
        会话总数以及最近 10 条会话的摘要信息。

    参数:
        user_id (int): 用户主键 ID，来自 URL 路径参数 /{user_id}。
        request (Request): FastAPI 的请求对象。

    返回值:
        UserDetail: 用户详情对象，包含:
            - id, external_id, nickname: 用户基本标识信息
            - health_profile: 健康画像字典（无数据时为空字典 {}）
            - session_count: 会话总数
            - last_active_at, created_at: 时间字段（ISO 8601 格式）
            - recent_sessions: 最近 10 条会话的列表，每条含 session_id、status、created_at

    异常:
        HTTPException(404): 当 user_id 对应的用户在数据库中不存在时抛出。
    """
    # 获取数据库异步会话
    async with get_db() as db:
        # 按主键查询用户，若不存在则返回 404
        user = await db.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")

        # 统计该用户的会话总数
        sess_count = (
            await db.execute(
                select(func.count(SessionModel.id)).where(
                    SessionModel.user_id == user_id
                )
            )
        ).scalar() or 0

        # 查询用户最近 10 条会话，按创建时间降序排列（最新的在前）
        recent = (
            await db.execute(
                select(
                    SessionModel.session_id,
                    SessionModel.status,
                    SessionModel.created_at,
                )
                .where(SessionModel.user_id == user_id)
                .order_by(SessionModel.created_at.desc())
                .limit(10)
            )
        ).all()

        # 组装 UserDetail 响应对象
        return UserDetail(
            id=user.id,
            external_id=user.external_id,
            nickname=user.nickname,
            # 健康画像可能为 None（数据库默认值），此时返回空字典 {}
            health_profile=user.health_profile or {},
            session_count=sess_count,
            last_active_at=_iso(user.last_active_at),
            created_at=_iso(user.created_at),
            # 将每条会话记录转为字典格式，时间字段统一用 _iso() 序列化
            recent_sessions=[
                {"session_id": r.session_id, "status": r.status, "created_at": _iso(r.created_at)}
                for r in recent
            ],
        )


@router.get("/{user_id}/sessions")
async def get_user_sessions(
    user_id: int,
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    分页获取某个用户的全部会话列表，每条会话附带消息数量统计。

    这一步的作用:
        管理员查看某个用户详情后，可能需要进一步浏览该用户的所有历史会话。
        此接口会分页返回该用户的会话列表，并通过 LEFT JOIN 关联 Message 表
        统计每条会话包含的消息数（message_count），帮助管理员快速判断会话的交互深度。

    参数:
        user_id (int): 用户主键 ID，来自 URL 路径参数 /{user_id}/sessions。
        request (Request): FastAPI 的请求对象。
        page (int): 页码，从 1 开始，最小值为 1。默认第 1 页。
        page_size (int): 每页显示的记录数，范围 1~100。默认每页 20 条。

    返回值:
        PaginatedResponse[dict]: 分页响应对象，其中 items 是字典列表，每条包含:
            - session_id: 会话的唯一标识符
            - status: 会话状态（如 active, completed, cancelled 等）
            - created_at: 会话创建时间（ISO 8601 格式字符串）
            - message_count: 该会话中的消息总数

    异常:
        HTTPException(404): 当 user_id 对应的用户在数据库中不存在时抛出。
    """
    # 获取数据库异步会话
    async with get_db() as db:
        # 先验证用户是否存在，不存在则返回 404
        user = await db.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")

        # 构建基础查询：关联 Session 和 Message 表，统计每条会话的消息数
        # 使用 LEFT JOIN 确保没有消息的会话也会出现在结果中（message_count = 0）
        base = (
            select(
                SessionModel.session_id,
                SessionModel.status,
                SessionModel.created_at,
                # COUNT 聚合函数统计每条会话的消息数量，label 为后续引用提供别名
                func.count(Message.id).label("message_count"),
            )
            # 左连接 Message 表：Session.id = Message.session_id
            .outerjoin(Message, SessionModel.id == Message.session_id)
            # 只查询该用户的会话
            .where(SessionModel.user_id == user_id)
            # 按 Session.id 分组，配合 COUNT 统计每个会话的消息数
            .group_by(SessionModel.id)
            # 按创建时间降序排列（最新的会话在前）
            .order_by(SessionModel.created_at.desc())
        )

        # 统计符合条件的会话总数（将 base 查询作为子查询进行 count）
        count_q = select(func.count()).select_from(base.subquery())
        total = (await db.execute(count_q)).scalar() or 0

        # 计算偏移量，执行分页查询
        offset = (page - 1) * page_size
        rows = (await db.execute(base.offset(offset).limit(page_size))).all()

        # 遍历查询结果，组装为字典列表（方便 JSON 序列化）
        items = [
            {
                "session_id": r.session_id,
                "status": r.status,
                "created_at": _iso(r.created_at),
                "message_count": r.message_count,
            }
            for r in rows
        ]

        # 返回分页响应
        return PaginatedResponse(
            items=items, total=total, page=page, page_size=page_size,
        )
