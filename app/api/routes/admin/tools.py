"""
Admin 工具管理 — Agent 工具注册与状态管理。

GET  /api/v1/admin/tools                     — 工具列表
GET  /api/v1/admin/tools/{name}              — 工具详情
PUT  /api/v1/admin/tools/{name}              — 更新元数据
PUT  /api/v1/admin/tools/{name}/status       — 启用/停用
GET  /api/v1/admin/tools/{name}/stats        — 调用统计

Phase 1: 只读展示 + 启停，不修改 parameters_schema。
"""

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.routes.admin.schemas import PaginatedResponse
from app.db.database import get_db
from app.db.models import Tool

router = APIRouter(prefix="/tools", tags=["admin"])


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts else None


# ── Schema ──────────────────────────────────────────────────


class ToolListItem(BaseModel):
    id: int
    name: str
    display_name: str
    description: str
    capabilities: list
    fallback_tools: list
    timeout_ms: int
    retry_count: int
    status: str
    updated_at: str | None


class ToolUpdate(BaseModel):
    display_name: str | None = None
    description: str | None = None
    timeout_ms: int | None = None
    retry_count: int | None = None


class ToolStatusUpdate(BaseModel):
    status: str = Field(..., description="active|inactive|deprecated")


class ToolStatsOut(BaseModel):
    name: str
    total_calls: int = 0
    success_rate: float = 0.0
    avg_latency_ms: float = 0.0


# ── Routes ──────────────────────────────────────────────────


@router.get("")
async def list_tools(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    status: str | None = Query(default=None),
) -> PaginatedResponse[ToolListItem]:
    """分页查询工具列表。"""
    async with get_db() as db:
        base = select(Tool)
        if status:
            base = base.where(Tool.status == status)
        base = base.order_by(Tool.name.asc())

        count_q = select(func.count()).select_from(base.subquery())
        total = (await db.execute(count_q)).scalar() or 0

        offset = (page - 1) * page_size
        rows = (
            await db.execute(base.offset(offset).limit(page_size))
        ).scalars().all()

        items = [
            ToolListItem(
                id=r.id,
                name=r.name,
                display_name=r.display_name,
                description=r.description,
                capabilities=r.capabilities,
                fallback_tools=r.fallback_tools,
                timeout_ms=r.timeout_ms,
                retry_count=r.retry_count,
                status=r.status,
                updated_at=_iso(r.updated_at),
            )
            for r in rows
        ]

        return PaginatedResponse(
            items=items, total=total, page=page, page_size=page_size,
        )


@router.get("/{name}")
async def get_tool(name: str) -> dict:
    """获取工具详情（含 parameters_schema）。"""
    async with get_db() as db:
        stmt = select(Tool).where(Tool.name == name)
        result = await db.execute(stmt)
        tool = result.scalar_one_or_none()

        if tool is None:
            raise HTTPException(status_code=404, detail=f"Tool not found: {name}")

        return {
            "id": tool.id,
            "name": tool.name,
            "display_name": tool.display_name,
            "description": tool.description,
            "parameters_schema": tool.parameters_schema,
            "capabilities": tool.capabilities,
            "fallback_tools": tool.fallback_tools,
            "timeout_ms": tool.timeout_ms,
            "retry_count": tool.retry_count,
            "status": tool.status,
            "created_at": _iso(tool.created_at),
            "updated_at": _iso(tool.updated_at),
        }


@router.put("/{name}")
async def update_tool(name: str, body: ToolUpdate) -> ToolListItem:
    """更新工具元数据。"""
    async with get_db() as db:
        stmt = select(Tool).where(Tool.name == name)
        result = await db.execute(stmt)
        tool = result.scalar_one_or_none()

        if tool is None:
            raise HTTPException(status_code=404, detail=f"Tool not found: {name}")

        if body.display_name is not None:
            tool.display_name = body.display_name
        if body.description is not None:
            tool.description = body.description
        if body.timeout_ms is not None:
            tool.timeout_ms = body.timeout_ms
        if body.retry_count is not None:
            tool.retry_count = body.retry_count

        tool.updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(tool)

        return ToolListItem(
            id=tool.id, name=tool.name, display_name=tool.display_name,
            description=tool.description, capabilities=tool.capabilities,
            fallback_tools=tool.fallback_tools, timeout_ms=tool.timeout_ms,
            retry_count=tool.retry_count, status=tool.status,
            updated_at=_iso(tool.updated_at),
        )


@router.put("/{name}/status")
async def update_tool_status(name: str, body: ToolStatusUpdate) -> dict:
    """启用 / 停用工具。"""
    async with get_db() as db:
        stmt = select(Tool).where(Tool.name == name)
        result = await db.execute(stmt)
        tool = result.scalar_one_or_none()

        if tool is None:
            raise HTTPException(status_code=404, detail=f"Tool not found: {name}")

        old_status = tool.status
        tool.status = body.status
        tool.updated_at = datetime.now(timezone.utc)
        await db.commit()

        return {
            "success": True,
            "tool": name,
            "previous_status": old_status,
            "current_status": body.status,
        }


@router.get("/{name}/stats")
async def tool_stats(name: str) -> ToolStatsOut:
    """获取工具的调用统计。

    注意：精确的工具级统计需要 LLMCallLog 支持 tool_name 字段（Phase 2）。
    当前返回 0 值占位。使用 /admin/llm/overview 查看 LLM 总体用量。
    """
    async with get_db() as db:
        stmt = select(Tool).where(Tool.name == name)
        result = await db.execute(stmt)
        tool = result.scalar_one_or_none()

        if tool is None:
            raise HTTPException(status_code=404, detail=f"Tool not found: {name}")

        return ToolStatsOut(
            name=name,
            total_calls=0,
            success_rate=0.0,
            avg_latency_ms=0.0,
        )
