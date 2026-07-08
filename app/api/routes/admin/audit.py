"""
Admin 审计中心模块 — 操作审计日志的写入与查询。

本模块是后台管理系统的安全审计核心，负责记录管理员的所有写操作
（创建、更新、删除、启用、禁用等），以便事后追溯和合规审查。

=== 整体功能概览 ===

1. 审计日志写入（两种方式，按需选择）：
   - 方式一（推荐）：在业务逻辑中显式调用 `await audit_log(db, ...)`，
     可以精确记录变更内容（changes 字段），适用于所有写操作。
   - 方式二（兜底）：通过 `AuditLogMiddleware` 中间件自动捕获所有
     admin 路径下的写请求，无需在每处业务代码中手动调用，
     但记录的 changes 信息较粗略（仅含请求元数据，不含请求体）。

2. 审计日志查询：
   - GET /api/v1/admin/audit — 分页查询审计日志，支持按管理员用户名、
     操作类型、资源类型、日期范围等条件筛选。

=== 使用方式 ===

写入示例（方式一，显式调用）：:

    async with get_db() as db:
        drug = Drug(...)
        db.add(drug)
        await db.commit()
        await audit_log(
            db, action="create", resource_type="drug",
            resource_id=str(drug.id),
            changes={"generic_name": drug.generic_name},
        )

中间件注册示例（方式二，自动兜底）：:

    from app.api.routes.admin.audit import AuditLogMiddleware
    app.add_middleware(AuditLogMiddleware)

=== 隐私合规说明（个保法 / GDPR） ===

本模块采集客户端 IP 地址仅用于安全审计目的，采集依据为
"履行法定职责或法定义务所必需"（《个人信息保护法》第 13 条）。
IP 地址仅存储在 admin_audit_logs 表中，不用于用户画像或行为分析。
如需删除，可通过审计日志清理接口移除。
"""

import json
import logging
import time as time_module  # 别名为 time_module，避免与标准库 time 冲突
from datetime import datetime, timezone
from typing import Awaitable, Callable

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from app.api.routes.admin.schemas import PaginatedResponse
from app.db.database import get_db
from app.db.models import AdminAuditLog

# 模块级日志记录器，用于输出调试级别的审计写入失败信息
logger = logging.getLogger(__name__)

# 创建审计相关的 API 路由器，所有端点路径以 /audit 为前缀
router = APIRouter(prefix="/audit", tags=["admin"])


def _iso(ts: datetime | None) -> str | None:
    """将 datetime 对象转换为 ISO 8601 格式字符串。

    这一步的作用：
        将数据库中的 datetime 类型字段统一转换为 ISO 8601 字符串格式，
        便于 JSON 序列化后在 API 响应中返回给前端。

    Args:
        ts: 待转换的 datetime 对象，可以为 None。

    Returns:
        ISO 8601 格式的时间字符串（如 "2026-07-07T12:00:00+00:00"），
        如果输入为 None 则返回 None。
    """
    return ts.isoformat() if ts else None


# ──────────────────────────────────────────────────────────────
# Schema（数据模型定义）
# ──────────────────────────────────────────────────────────────

class AuditItem(BaseModel):
    """审计日志条目的 API 响应模型。

    用于序列化 AdminAuditLog 数据库记录，作为分页查询接口的响应体。
    """
    id: int                      # 审计日志记录的唯一 ID
    admin_user: str              # 执行操作的管理员用户名
    action: str                  # 操作类型（create / update / delete / activate / deactivate）
    resource_type: str           # 被操作的资源类型（drug / prompt / skill / tool / config 等）
    resource_id: str | None      # 被操作的资源 ID（可能为空）
    changes: dict | None         # 变更内容的 JSON 对象（可能为空）
    ip_address: str | None       # 操作来源的客户端 IP 地址（可能为空）
    created_at: str | None       # 操作发生时间（ISO 8601 字符串格式，可能为空）


# ──────────────────────────────────────────────────────────────
# 审计写入 Helper（显式调用方式）
# ──────────────────────────────────────────────────────────────

async def audit_log(
    db: AsyncSession,
    *,
    admin_user: str = "admin",
    action: str,          # 操作类型："create" | "update" | "delete" | "activate" | "deactivate"
    resource_type: str,   # 资源类型："drug" | "prompt" | "skill" | "tool" | "config" 等
    resource_id: str | None = None,
    changes: dict | None = None,
    ip_address: str | None = None,
) -> None:
    """向数据库写入一条审计日志（fire-and-forget 风格，失败时静默忽略）。

    这一步的作用：
        在管理员执行写操作（新建、编辑、删除、启用/禁用）后，
        将操作记录持久化到 admin_audit_logs 表中，用于事后审计追溯。
        采用 fire-and-forget 模式：即使审计日志写入失败，也不会
        影响主业务流程（不会向上抛出异常）。

    新增 admin 模块时，只需在写操作中调用此函数即可自动接入审计。

    用法示例::

        async with get_db() as db:
            drug = Drug(...)
            db.add(drug)
            await db.commit()
            await audit_log(
                db, action="create", resource_type="drug",
                resource_id=str(drug.id),
                changes={"generic_name": drug.generic_name},
            )

    Args:
        db: 异步 SQLAlchemy 数据库会话，用于执行 INSERT 操作。
        admin_user: 执行操作的管理员用户名，默认为 "admin"（Phase 2 后从 JWT 提取）。
        action: 操作类型，如 "create"、"update"、"delete"、"activate"、"deactivate"。
        resource_type: 被操作的资源类型，如 "drug"、"prompt"、"skill"、"tool"、"config"。
        resource_id: 被操作的资源唯一标识符（如药品 ID），可以为 None。
        changes: 变更内容的字典（如 {"generic_name": "布洛芬"}），可以为 None。
        ip_address: 操作来源的客户端 IP 地址，可以为 None。
    """
    try:
        # 构造审计日志 ORM 对象并添加到数据库会话
        db.add(AdminAuditLog(
            admin_user=admin_user,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            changes=changes,
            ip_address=ip_address,
        ))
        # 提交事务，将审计日志持久化到数据库
        await db.commit()
    except Exception:
        # 审计写入失败时仅记录 debug 日志，不向上层抛出异常，
        # 确保审计功能不影响主业务流程
        logger.debug("Audit log write failed for %s/%s", resource_type, action, exc_info=True)


# ──────────────────────────────────────────────────────────────
# 中间件（自动兜底方式）
# ──────────────────────────────────────────────────────────────

# URL 前缀 → 资源类型映射表
# 中间件通过请求 URL 前缀来自动推断被操作的资源类型，
# 每条规则格式为 (URL前缀, 对应的资源类型名称)。
# 新增 admin 子模块时，需要在此处添加对应的映射规则。
_URL_RESOURCE_MAP: list[tuple[str, str]] = [
    ("/api/v1/admin/database/drugs", "drug"),
    ("/api/v1/admin/database/inventory", "inventory"),
    ("/api/v1/admin/database/weights", "weight_config"),
    ("/api/v1/admin/llm/models", "model_config"),
    ("/api/v1/admin/prompts", "prompt"),
    ("/api/v1/admin/skills", "skill"),
    ("/api/v1/admin/tools", "tool"),
    ("/api/v1/admin/risk-keywords", "risk_keyword"),
    ("/api/v1/admin/risk-alerts", "risk_alert"),
    ("/api/v1/admin/config", "system_config"),
    ("/api/v1/admin/feedback", "feedback"),
]

# HTTP 方法 → 操作类型映射表
# 将 RESTful HTTP 方法映射为审计日志中的操作类型：
# POST 表示创建、PUT/PATCH 表示更新、DELETE 表示删除
_HTTP_METHOD_ACTION: dict[str, str] = {
    "POST": "create",
    "PUT": "update",
    "PATCH": "update",
    "DELETE": "delete",
}


def _infer_resource(url_path: str) -> str | None:
    """从请求 URL 路径推断被操作的资源类型。

    这一步的作用：
        中间件无法访问请求体，只能通过 URL 路径前缀来推断
        当前请求操作的是哪种资源（药品、提示词、技能、工具等）。
        遍历 _URL_RESOURCE_MAP 映射表，找到第一个匹配的 URL 前缀
        并返回对应的资源类型名称。

    Args:
        url_path: HTTP 请求的 URL 路径（如 "/api/v1/admin/database/drugs/42"）。

    Returns:
        匹配到的资源类型名称（如 "drug"），如果没有匹配到则返回 None。
    """
    for prefix, resource in _URL_RESOURCE_MAP:
        if url_path.startswith(prefix):
            return resource
    return None


def _extract_resource_id(url_path: str) -> str | None:
    """从 URL 路径中提取资源 ID（路径中第一个纯数字段）。

    这一步的作用：
        从 URL 中自动提取被操作资源的 ID，例如对于路径
        "/api/v1/admin/database/drugs/42" 会提取出 "42"。
        采用从路径末尾向前遍历的方式，找到第一个全数字段
        即认为是资源 ID。

    Args:
        url_path: HTTP 请求的 URL 路径（如 "/api/v1/admin/database/drugs/42"）。

    Returns:
        提取到的资源 ID 字符串（如 "42"），如果没有找到数字段则返回 None。
    """
    import re
    # 去除末尾斜杠后按 "/" 分割路径
    parts = url_path.rstrip("/").split("/")
    # 从路径末尾向前遍历，找到第一个全数字段
    for part in reversed(parts):
        if re.match(r"^\d+$", part):
            return part
    return None


class AuditLogMiddleware(BaseHTTPMiddleware):
    """审计日志中间件 — 自动为所有 admin 写操作记录审计日志。

    这一步的作用：
        作为审计日志写入的兜底方案，自动拦截所有 admin 路径下的
        写请求（POST/PUT/PATCH/DELETE），在请求成功返回后自动
        写入审计日志。无需在每个端点中手动调用 audit_log()。

    与显式调用的区别：
        - 显式调用 audit_log() 可获得更精确的 changes 信息（包含实际变更数据）。
        - 中间件记录的 changes 仅包含请求元数据（HTTP 方法、路径、状态码、
          耗时等），不含请求体中的具体业务数据。

    **隐私合规说明** (个保法/GDPR):
        本中间件采集 ``request.client.host``（客户端 IP 地址）用于安全审计目的。
        采集依据: 履行法定职责或法定义务所必需（《个人信息保护法》第 13 条）。
        IP 地址仅存储在 admin_audit_logs 表中，不用于用户画像或行为分析。
        如需删除，可通过审计日志清理接口移除（Phase 2）。

    用法::

        from app.api.routes.admin.audit import AuditLogMiddleware
        app.add_middleware(AuditLogMiddleware)
    """

    def __init__(self, app: ASGIApp) -> None:
        """初始化中间件。

        Args:
            app: 被包装的 ASGI 应用实例。
        """
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """拦截并处理每个 HTTP 请求，对 admin 写操作自动记录审计日志。

        这一步的作用：
            1. 判断当前请求是否为 admin 路径下的写操作（POST/PUT/PATCH/DELETE）。
            2. 将请求传递给下游处理（实际的端点逻辑）。
            3. 在请求成功（HTTP 2xx）返回后，自动构造并写入审计日志记录。

        Args:
            request: FastAPI/Starlette 的 Request 对象，包含请求方法、路径、客户端信息等。
            call_next: ASGI 的下一步调用函数，用于将请求传递给后续中间件或端点处理。

        Returns:
            下游处理返回的 Response 对象，不做任何修改。
        """
        # 获取请求的 URL 路径和 HTTP 方法
        path = request.url.path
        method = request.method

        # 判断是否需要审计：必须是 admin 路径 且 是写操作
        should_audit = (
            path.startswith("/api/v1/admin/")
            and method in _HTTP_METHOD_ACTION
        )

        # 记录请求处理开始时间（用于计算耗时）
        t0 = time_module.monotonic()
        # 将请求传递给下游处理（实际执行业务逻辑）
        response = await call_next(request)
        # 计算请求处理耗时（毫秒）
        elapsed_ms = (time_module.monotonic() - t0) * 1000

        # 仅在请求成功（HTTP 2xx）时才写入审计日志
        if should_audit and 200 <= response.status_code < 300:
            # 从 HTTP 方法推断操作类型
            action = _HTTP_METHOD_ACTION.get(method, "unknown")
            # 从 URL 路径推断资源类型
            resource_type = _infer_resource(path) or "unknown"
            # 从 URL 路径提取资源 ID
            resource_id = _extract_resource_id(path)

            try:
                # 获取独立的数据库会话并写入审计日志
                async with get_db() as db:
                    db.add(AdminAuditLog(
                        admin_user="admin",  # Phase 2: 从 JWT token 提取真实用户名
                        action=action,
                        resource_type=resource_type,
                        resource_id=resource_id,
                        # 中间件记录的 changes 仅包含请求元数据，不含请求体
                        changes={
                            "method": method,
                            "path": path,
                            "status_code": response.status_code,
                            "elapsed_ms": round(elapsed_ms, 1),
                        },
                        # 从请求中提取客户端 IP 地址（用于安全审计）
                        ip_address=request.client.host if request.client else None,
                    ))
                    await db.commit()
            except Exception:
                # 审计写入失败时仅记录 debug 日志，不影响正常响应
                logger.debug("Audit middleware write failed", exc_info=True)

        # 返回原始响应，不做任何修改
        return response


# ──────────────────────────────────────────────────────────────
# 查询端点（审计日志分页查询）
# ──────────────────────────────────────────────────────────────


@router.get("")
async def list_audit_logs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    admin_user: str | None = Query(default=None),
    action: str | None = Query(default=None),
    resource_type: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
) -> PaginatedResponse[AuditItem]:
    """分页查询审计日志，支持多条件筛选。

    这一步的作用：
        为后台管理界面提供审计日志的分页查询能力。管理员可以通过
        用户名、操作类型、资源类型、日期范围等条件组合筛选审计记录，
        结果按创建时间倒序排列（最新的在前）。

    Args:
        page: 页码，从 1 开始，默认为第 1 页。
        page_size: 每页条数，取值范围 1~100，默认为 20。
        admin_user: 按管理员用户名筛选（可选，精确匹配）。
        action: 按操作类型筛选（可选，如 "create"、"update"、"delete"）。
        resource_type: 按资源类型筛选（可选，如 "drug"、"prompt"、"skill"）。
        date_from: 按起始日期筛选（可选，ISO 8601 格式，如 "2026-01-01"）。
        date_to: 按截止日期筛选（可选，ISO 8601 格式，如 "2026-12-31"）。

    Returns:
        分页响应对象 PaginatedResponse，包含：
        - items: 当前页的审计日志条目列表（AuditItem 类型）
        - total: 符合条件的记录总数
        - page: 当前页码
        - page_size: 每页条数
    """
    async with get_db() as db:
        # 构建基础查询：从 admin_audit_logs 表查询
        base = select(AdminAuditLog)

        # 按管理员用户名筛选（精确匹配）
        if admin_user:
            base = base.where(AdminAuditLog.admin_user == admin_user)
        # 按操作类型筛选
        if action:
            base = base.where(AdminAuditLog.action == action)
        # 按资源类型筛选
        if resource_type:
            base = base.where(AdminAuditLog.resource_type == resource_type)
        # 按起始日期筛选（created_at >= date_from）
        if date_from:
            base = base.where(AdminAuditLog.created_at >= datetime.fromisoformat(date_from))
        # 按截止日期筛选（created_at <= date_to）
        if date_to:
            base = base.where(AdminAuditLog.created_at <= datetime.fromisoformat(date_to))

        # 按创建时间倒序排列（最新的记录在最前面）
        base = base.order_by(AdminAuditLog.created_at.desc())

        # 统计符合条件的总记录数（用于前端分页组件）
        count_q = select(func.count()).select_from(base.subquery())
        total = (await db.execute(count_q)).scalar() or 0

        # 计算当前页的偏移量
        offset = (page - 1) * page_size
        # 执行分页查询，获取当前页的数据
        rows = (
            await db.execute(base.offset(offset).limit(page_size))
        ).scalars().all()

        # 将 ORM 对象转换为 API 响应模型（AuditItem）
        items = [
            AuditItem(
                id=r.id, admin_user=r.admin_user, action=r.action,
                resource_type=r.resource_type, resource_id=r.resource_id,
                changes=r.changes, ip_address=r.ip_address,
                created_at=_iso(r.created_at),  # datetime 转 ISO 字符串
            )
            for r in rows
        ]

        # 返回标准分页响应
        return PaginatedResponse(
            items=items, total=total, page=page, page_size=page_size,
        )
