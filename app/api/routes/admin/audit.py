"""
Admin 审计中心 — 操作审计日志写入 + 查询。

写入方式（按需选择）：
  1. `await audit_log(db, ...)` — 显式调用（最精确，推荐写操作使用）
  2. `AuditLogMiddleware` — 自动捕获所有 admin 写请求（兜底）

查询:
  GET  /api/v1/admin/audit  — 分页列表 + 筛选
"""

import json
import logging
import time as time_module
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

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/audit", tags=["admin"])


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts else None


# ──────────────────────────────────────────────────────────────
# Schema
# ──────────────────────────────────────────────────────────────

class AuditItem(BaseModel):
    id: int
    admin_user: str
    action: str
    resource_type: str
    resource_id: str | None
    changes: dict | None
    ip_address: str | None
    created_at: str | None


# ──────────────────────────────────────────────────────────────
# 审计写入 Helper
# ──────────────────────────────────────────────────────────────

async def audit_log(
    db: AsyncSession,
    *,
    admin_user: str = "admin",
    action: str,          # "create" | "update" | "delete" | "activate" | "deactivate"
    resource_type: str,   # "drug" | "prompt" | "skill" | "tool" | "config" | ...
    resource_id: str | None = None,
    changes: dict | None = None,
    ip_address: str | None = None,
) -> None:
    """写一条审计日志（fire-and-forget 风格，失败静默忽略）。

    用法示例::

        async with get_db() as db:
            drug = Drug(...)
            db.add(drug)
            await db.commit()
            await audit_log(
                db, action="create", resource_type="drug",
                resource_id=str(drug.id), changes={"generic_name": drug.generic_name},
            )

    新增 admin 模块时，在写操作中调用此函数即可自动接入审计。
    """
    try:
        db.add(AdminAuditLog(
            admin_user=admin_user,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            changes=changes,
            ip_address=ip_address,
        ))
        await db.commit()
    except Exception:
        logger.debug("Audit log write failed for %s/%s", resource_type, action, exc_info=True)


# ──────────────────────────────────────────────────────────────
# 中间件（自动兜底）
# ──────────────────────────────────────────────────────────────

# 已知的 resource_type 提取规则：从 URL 前缀推断资源类型
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

_HTTP_METHOD_ACTION: dict[str, str] = {
    "POST": "create",
    "PUT": "update",
    "PATCH": "update",
    "DELETE": "delete",
}


def _infer_resource(url_path: str) -> str | None:
    """从请求 URL 推断资源类型。"""
    for prefix, resource in _URL_RESOURCE_MAP:
        if url_path.startswith(prefix):
            return resource
    return None


def _extract_resource_id(url_path: str) -> str | None:
    """从 URL 路径提取资源 ID（路径中第一个数字段）。"""
    import re
    parts = url_path.rstrip("/").split("/")
    for part in reversed(parts):
        if re.match(r"^\d+$", part):
            return part
    return None


class AuditLogMiddleware(BaseHTTPMiddleware):
    """自动为所有 admin 写操作记录审计日志。

    这是兜底方案——端点内显式调用 audit_log() 可获得更精确的 changes 信息。
    中间件记录的 changes 仅包含请求路径和方法，不含请求体。

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
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # 只处理 admin 路径的写操作
        path = request.url.path
        method = request.method

        should_audit = (
            path.startswith("/api/v1/admin/")
            and method in _HTTP_METHOD_ACTION
        )

        t0 = time_module.monotonic()
        response = await call_next(request)
        elapsed_ms = (time_module.monotonic() - t0) * 1000

        if should_audit and 200 <= response.status_code < 300:
            action = _HTTP_METHOD_ACTION.get(method, "unknown")
            resource_type = _infer_resource(path) or "unknown"
            resource_id = _extract_resource_id(path)

            try:
                async with get_db() as db:
                    db.add(AdminAuditLog(
                        admin_user="admin",  # Phase 2: 从 JWT token 提取
                        action=action,
                        resource_type=resource_type,
                        resource_id=resource_id,
                        changes={
                            "method": method,
                            "path": path,
                            "status_code": response.status_code,
                            "elapsed_ms": round(elapsed_ms, 1),
                        },
                        ip_address=request.client.host if request.client else None,
                    ))
                    await db.commit()
            except Exception:
                logger.debug("Audit middleware write failed", exc_info=True)

        return response


# ──────────────────────────────────────────────────────────────
# 查询端点
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
    """分页查询审计日志。"""
    async with get_db() as db:
        base = select(AdminAuditLog)

        if admin_user:
            base = base.where(AdminAuditLog.admin_user == admin_user)
        if action:
            base = base.where(AdminAuditLog.action == action)
        if resource_type:
            base = base.where(AdminAuditLog.resource_type == resource_type)
        if date_from:
            base = base.where(AdminAuditLog.created_at >= datetime.fromisoformat(date_from))
        if date_to:
            base = base.where(AdminAuditLog.created_at <= datetime.fromisoformat(date_to))

        base = base.order_by(AdminAuditLog.created_at.desc())

        count_q = select(func.count()).select_from(base.subquery())
        total = (await db.execute(count_q)).scalar() or 0

        offset = (page - 1) * page_size
        rows = (
            await db.execute(base.offset(offset).limit(page_size))
        ).scalars().all()

        items = [
            AuditItem(
                id=r.id, admin_user=r.admin_user, action=r.action,
                resource_type=r.resource_type, resource_id=r.resource_id,
                changes=r.changes, ip_address=r.ip_address,
                created_at=_iso(r.created_at),
            )
            for r in rows
        ]

        return PaginatedResponse(
            items=items, total=total, page=page, page_size=page_size,
        )
