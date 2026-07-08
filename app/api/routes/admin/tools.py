"""
Admin 工具管理模块 — Agent 工具注册与状态管理。

本模块负责提供管理后台中与 Agent 工具（Tool）相关的 REST API 接口，
支持对已注册工具的查询、元数据更新、启用/停用以及调用统计查看等功能。

=== 可用接口一览 ===
GET  /api/v1/admin/tools                     — 分页查询工具列表，支持按状态筛选
GET  /api/v1/admin/tools/{name}              — 获取单个工具的完整详情（含参数 Schema）
PUT  /api/v1/admin/tools/{name}              — 更新工具的元数据（显示名称、描述、超时等）
PUT  /api/v1/admin/tools/{name}/status       — 启用 / 停用指定工具
GET  /api/v1/admin/tools/{name}/stats        — 获取指定工具的调用统计

=== 设计说明 ===
Phase 1: 只读展示 + 启停，不修改 parameters_schema。
后续版本将扩展精确的工具级调用统计（需 LLMCallLog 支持 tool_name 字段）。
"""

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.routes.admin.schemas import PaginatedResponse
from app.db.database import get_db
from app.db.models import Tool

# 创建工具管理路由组，所有路由前缀为 /tools，归类到 admin 标签下
router = APIRouter(prefix="/tools", tags=["admin"])


def _iso(ts: datetime | None) -> str | None:
    """
    将 datetime 对象转换为 ISO 8601 格式字符串。

    这一步的作用：统一时间戳的输出格式，确保 API 返回的时间字段
    符合国际标准（ISO 8601），便于前端和其他系统解析。

    参数：
        ts: 需要转换的 datetime 对象，可以为 None。

    返回值：
        如果 ts 不为 None，返回 ISO 8601 格式的时间字符串；
        如果 ts 为 None，返回 None。
    """
    return ts.isoformat() if ts else None


# ── Schema ──────────────────────────────────────────────────
# 以下 Pydantic 模型定义了各接口的请求体与响应体数据结构。


class ToolListItem(BaseModel):
    """
    工具列表项的数据结构（用于分页列表接口的响应）。

    每个字段对应 Tool 数据库表中的一列，用于在列表页展示工具的摘要信息。
    """
    id: int                        # 工具在数据库中的主键 ID
    name: str                      # 工具的唯一标识名称（如 "search_drug"）
    display_name: str              # 工具的显示名称，用于 UI 展示
    description: str               # 工具的功能描述文本
    capabilities: list             # 工具的能力标签列表（如 ["搜索", "计算"]）
    fallback_tools: list           # 备选工具列表，当前工具不可用时的降级方案
    timeout_ms: int                # 调用超时时间（毫秒）
    retry_count: int               # 失败重试次数
    status: str                    # 工具状态：active（启用）/ inactive（停用）/ deprecated（已废弃）
    updated_at: str | None         # 最后更新时间（ISO 8601 字符串），可能为空


class ToolUpdate(BaseModel):
    """
    更新工具元数据的请求体。

    所有字段均为可选，仅传入需要修改的字段即可，未传入的字段保持不变。
    """
    display_name: str | None = None    # 新的显示名称，不传则保持不变
    description: str | None = None     # 新的功能描述，不传则保持不变
    timeout_ms: int | None = None      # 新的超时时间（毫秒），不传则保持不变
    retry_count: int | None = None     # 新的重试次数，不传则保持不变


class ToolStatusUpdate(BaseModel):
    """
    更新工具状态的请求体。

    用于启用或停用某个工具，仅包含目标状态字段。
    """
    status: str = Field(..., description="active|inactive|deprecated")
    # status 取值说明：
    #   active     — 工具正常可用
    #   inactive   — 工具已停用，Agent 不会调用它
    #   deprecated — 工具已废弃，将在未来版本中移除


class ToolStatsOut(BaseModel):
    """
    工具调用统计的响应体。

    包含工具的基本调用指标，用于管理后台展示工具的使用情况。
    注意：精确的工具级统计需 Phase 2 实现，当前返回默认零值。
    """
    name: str                      # 工具名称
    total_calls: int = 0           # 总调用次数
    success_rate: float = 0.0      # 调用成功率（0.0 ~ 1.0）
    avg_latency_ms: float = 0.0    # 平均调用延迟（毫秒）


# ── Routes ──────────────────────────────────────────────────
# 以下为各 REST API 路由处理函数的实现。


@router.get("")
async def list_tools(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    status: str | None = Query(default=None),
) -> PaginatedResponse[ToolListItem]:
    """
    分页查询工具列表。

    这一步的作用：返回已注册 Agent 工具的分页列表，支持按状态筛选。
    管理后台通过此接口展示所有工具及其基本元数据，方便运维人员浏览和查找。

    参数：
        request: FastAPI 请求对象，用于获取请求上下文。
        page: 当前页码，从 1 开始，默认第 1 页。
        page_size: 每页返回的记录数，范围 1~100，默认 20 条。
        status: 可选的状态筛选条件（active / inactive / deprecated），不传则返回全部。

    返回值：
        PaginatedResponse[ToolListItem]: 包含工具列表、总记录数和分页信息的分页响应对象。
    """
    async with get_db() as db:
        # 1. 构建基础查询：从 Tool 表中选取所有列
        base = select(Tool)
        # 2. 如果传入了 status 筛选条件，添加 WHERE 子句
        if status:
            base = base.where(Tool.status == status)
        # 3. 按工具名称升序排列，保证分页结果稳定
        base = base.order_by(Tool.name.asc())

        # 4. 统计符合条件的总记录数（用于前端分页组件显示总页数）
        count_q = select(func.count()).select_from(base.subquery())
        total = (await db.execute(count_q)).scalar() or 0

        # 5. 计算偏移量，执行分页查询
        offset = (page - 1) * page_size
        rows = (
            await db.execute(base.offset(offset).limit(page_size))
        ).scalars().all()

        # 6. 将数据库行对象转换为 API 响应模型（ToolListItem）
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

        # 7. 返回分页响应
        return PaginatedResponse(
            items=items, total=total, page=page, page_size=page_size,
        )


@router.get("/{name}")
async def get_tool(name: str) -> dict:
    """
    获取单个工具的完整详情（含参数 Schema）。

    这一步的作用：根据工具名称查询并返回该工具的完整元数据，
    包括 parameters_schema（参数定义 JSON），供管理后台查看和编辑。

    参数：
        name: 工具的唯一标识名称（路径参数），如 "search_drug"。

    返回值：
        dict: 包含工具所有字段的字典，包括 id、name、display_name、
              description、parameters_schema、capabilities、fallback_tools、
              timeout_ms、retry_count、status、created_at、updated_at。

    异常：
        HTTPException(404): 当指定的工具名称在数据库中不存在时抛出。
    """
    async with get_db() as db:
        # 1. 按工具名称精确查询（name 字段为唯一索引）
        stmt = select(Tool).where(Tool.name == name)
        result = await db.execute(stmt)
        tool = result.scalar_one_or_none()

        # 2. 工具不存在则返回 404 错误
        if tool is None:
            raise HTTPException(status_code=404, detail=f"Tool not found: {name}")

        # 3. 返回完整的工具信息字典（包含列表接口不返回的 parameters_schema 和 created_at）
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
    """
    更新工具的元数据。

    这一步的作用：允许管理员修改工具的显示名称、描述、超时时间和重试次数。
    采用部分更新策略（PATCH-like PUT）——请求体中未传入的字段保持原值不变。

    参数：
        name: 工具的唯一标识名称（路径参数）。
        body: 工具更新请求体，包含可选的 display_name、description、
              timeout_ms、retry_count 字段。

    返回值：
        ToolListItem: 更新后的工具摘要信息（不包含 parameters_schema 等敏感/大字段）。

    异常：
        HTTPException(404): 当指定的工具名称不存在时抛出。
    """
    async with get_db() as db:
        # 1. 查询目标工具
        stmt = select(Tool).where(Tool.name == name)
        result = await db.execute(stmt)
        tool = result.scalar_one_or_none()

        # 2. 不存在则返回 404
        if tool is None:
            raise HTTPException(status_code=404, detail=f"Tool not found: {name}")

        # 3. 仅更新请求中明确传入的字段（非 None 表示用户想修改该字段）
        if body.display_name is not None:
            tool.display_name = body.display_name
        if body.description is not None:
            tool.description = body.description
        if body.timeout_ms is not None:
            tool.timeout_ms = body.timeout_ms
        if body.retry_count is not None:
            tool.retry_count = body.retry_count

        # 4. 更新修改时间戳并持久化到数据库
        tool.updated_at = datetime.now(timezone.utc)
        await db.commit()
        # 5. 刷新对象以获取数据库生成的默认值或触发器更新的字段
        await db.refresh(tool)

        # 6. 返回更新后的工具信息
        return ToolListItem(
            id=tool.id, name=tool.name, display_name=tool.display_name,
            description=tool.description, capabilities=tool.capabilities,
            fallback_tools=tool.fallback_tools, timeout_ms=tool.timeout_ms,
            retry_count=tool.retry_count, status=tool.status,
            updated_at=_iso(tool.updated_at),
        )


@router.put("/{name}/status")
async def update_tool_status(name: str, body: ToolStatusUpdate) -> dict:
    """
    启用或停用指定工具。

    这一步的作用：切换工具的运行状态（active / inactive / deprecated）。
    当工具被停用后，Agent 在执行任务时将不再调用该工具。
    接口同时返回状态变更前后的值，便于前端做确认提示。

    参数：
        name: 工具的唯一标识名称（路径参数）。
        body: 状态更新请求体，包含目标状态字段 status。

    返回值：
        dict: 包含以下字段的字典：
            - success (bool): 操作是否成功。
            - tool (str): 工具名称。
            - previous_status (str): 变更前的状态。
            - current_status (str): 变更后的状态。

    异常：
        HTTPException(404): 当指定的工具名称不存在时抛出。
    """
    async with get_db() as db:
        # 1. 查询目标工具
        stmt = select(Tool).where(Tool.name == name)
        result = await db.execute(stmt)
        tool = result.scalar_one_or_none()

        # 2. 不存在则返回 404
        if tool is None:
            raise HTTPException(status_code=404, detail=f"Tool not found: {name}")

        # 3. 记录旧状态，用于返回变更前后的对比
        old_status = tool.status
        # 4. 更新为新状态并记录修改时间
        tool.status = body.status
        tool.updated_at = datetime.now(timezone.utc)
        # 5. 提交事务持久化
        await db.commit()

        # 6. 返回操作结果，包含状态变更前后的值
        return {
            "success": True,
            "tool": name,
            "previous_status": old_status,
            "current_status": body.status,
        }


@router.get("/{name}/stats")
async def tool_stats(name: str) -> ToolStatsOut:
    """
    获取指定工具的调用统计信息。

    这一步的作用：返回该工具的总调用次数、成功率和平均延迟等指标，
    帮助管理员评估工具的使用情况和性能表现。

    参数：
        name: 工具的唯一标识名称（路径参数）。

    返回值：
        ToolStatsOut: 包含 name、total_calls、success_rate、avg_latency_ms 的统计对象。

    注意：
        精确的工具级调用统计需要 LLMCallLog 表支持 tool_name 字段（Phase 2 规划）。
        当前版本暂未实现统计数据的采集和聚合，所有统计字段均返回默认零值。
        如需查看 LLM 总体用量，请使用 /admin/llm/overview 接口。

    异常：
        HTTPException(404): 当指定的工具名称不存在时抛出。
    """
    async with get_db() as db:
        # 1. 仅验证工具是否存在，统计功能尚未实现
        stmt = select(Tool).where(Tool.name == name)
        result = await db.execute(stmt)
        tool = result.scalar_one_or_none()

        # 2. 工具不存在则返回 404
        if tool is None:
            raise HTTPException(status_code=404, detail=f"Tool not found: {name}")

        # 3. 返回默认零值占位（Phase 2 将替换为真实统计数据查询）
        return ToolStatsOut(
            name=name,
            total_calls=0,
            success_rate=0.0,
            avg_latency_ms=0.0,
        )
