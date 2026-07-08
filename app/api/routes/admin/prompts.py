"""
Admin Prompt 管理中心 — 版本管理与 CRUD（创建、读取、更新、删除）。

═══════════════════════════════════════════════════════════════════════════════
模块概述
═══════════════════════════════════════════════════════════════════════════════
本模块是 Prompt 管理后台的 RESTful API 层，负责 Prompt 模板的版本化存储和管理。
与直接写死在代码里的 Prompt 常量不同，管理员可以通过此接口在数据库中创建、查看、
激活和删除不同版本的 Prompt 模板，而不需要重新部署应用。

每个 Prompt 模板属于一个"角色"（role，如 dispatcher、consult、react 等），
同一个角色可以有多个版本，但同一时刻只有一个版本处于"激活"（is_active）状态。
删除采用软删除策略（设置 deleted_at 时间戳），保留历史记录以供审计。

═══════════════════════════════════════════════════════════════════════════════
API 端点一览
═══════════════════════════════════════════════════════════════════════════════
GET    /api/v1/admin/prompts              — 分页查询 Prompt 模板列表
GET    /api/v1/admin/prompts/{id}         — 获取单个 Prompt 模板详情
POST   /api/v1/admin/prompts              — 新增 Prompt 版本
PUT    /api/v1/admin/prompts/{id}/activate — 激活指定版本（停用同角色其他版本）
DELETE /api/v1/admin/prompts/{id}         — 软删除 Prompt 版本

═══════════════════════════════════════════════════════════════════════════════
阶段说明
═══════════════════════════════════════════════════════════════════════════════
Phase 1: 仅管理界面 CRUD，运行时不从数据库读取 Prompt（仍使用代码中的常量定义）。
后续阶段可以考虑在运行时从数据库动态加载激活的 Prompt 模板。
"""

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.routes.admin.schemas import PaginatedResponse
from app.db.database import get_db
from app.db.models import PromptTemplate

router = APIRouter(prefix="/prompts", tags=["admin"])


def _iso(ts: datetime | None) -> str | None:
    """将 datetime 对象转换为 ISO 8601 格式字符串，用于 JSON 序列化输出。

    参数:
        ts: datetime 对象，可能为 None（表示未设置或不存在该时间字段）。

    返回:
        ISO 格式的字符串（如 "2025-01-01T12:00:00"）；若传入 None 则返回 None。
    """
    return ts.isoformat() if ts else None


# ── Schema（数据模型 / 请求与响应结构体）──────────────────────────────────
# 以下 Pydantic 模型定义了 API 的输入输出格式，用于自动参数校验和序列化。


class PromptListItem(BaseModel):
    """Prompt 模板列表项（用于分页列表响应），仅展示关键摘要字段，
    不包含完整 Prompt 内容以减小响应体积。"""

    id: int
    """Prompt 模板在数据库中的主键 ID"""
    role: str
    """Prompt 角色名（如 dispatcher、consult、react、recommend 等）"""
    version: str
    """语义版本号（如 "1.0.0"）"""
    is_active: bool
    """是否处于激活状态（同一角色只有一个版本可激活）"""
    description: str
    """对该版本 Prompt 的简要描述"""
    updated_by: str
    """最后修改者标识（如用户名或 "system"）"""
    created_at: str | None
    """创建时间（ISO 8601 格式字符串），可能为 None"""


# 预定义的所有合法 Prompt 角色名集合，用于校验创建请求的 role 字段是否合法
_KNOWN_PROMPT_ROLES = frozenset({
    "dispatcher", "consult", "react", "recommend",
    "classifier", "generator", "safety_block",
})


class PromptCreate(BaseModel):
    """创建 Prompt 模板的请求体模型。"""

    role: str = Field(..., description="Prompt 角色名", min_length=1, max_length=50)
    """Prompt 所属角色，必须在 _KNOWN_PROMPT_ROLES 预定义集合中"""
    version: str = Field(..., description="语义版本号", min_length=1, max_length=20)
    """语义版本号，如 "1.0.0"，用于同角色下区分不同版本"""
    content: str = Field(..., description="Prompt 全文", min_length=1)
    """Prompt 模板的完整文本内容"""
    description: str = Field(default="", max_length=500)
    """该版本的描述信息，可选，最大 500 字符"""
    updated_by: str = Field(default="system", max_length=100)
    """创建者标识，默认为 "system"，最大 100 字符"""


class PromptDetail(BaseModel):
    """Prompt 模板详情响应模型，包含完整内容和所有元数据字段。"""

    id: int
    """Prompt 模板数据库主键 ID"""
    role: str
    """Prompt 角色名"""
    version: str
    """语义版本号"""
    content: str
    """Prompt 模板的完整文本内容"""
    is_active: bool
    """是否激活"""
    description: str
    """描述信息"""
    updated_by: str
    """最后修改者"""
    created_at: str | None
    """创建时间（ISO 8601 格式字符串），可能为 None"""


# ── Routes（路由 / API 端点实现）──────────────────────────────────────────
# 以下每个函数对应一个 HTTP 端点，处理请求、执行数据库操作、返回结构化响应。


@router.get("")
async def list_prompts(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    role: str | None = Query(default=None, description="按角色筛选"),
) -> PaginatedResponse[PromptListItem]:
    """分页查询 Prompt 模板列表，支持按角色筛选。

    参数:
        request: FastAPI 请求对象，包含请求上下文信息。
        page: 当前页码，从 1 开始，必须 >= 1。
        page_size: 每页返回的记录数，范围 1-100，默认 20。
        role: 可选的角色名筛选条件，传入后只返回该角色的 Prompt。

    返回:
        分页响应体，包含 Prompt 列表（items）、总数（total）、当前页和每页大小。
    """
    async with get_db() as db:
        # 步骤 1: 构建基础查询 — 查询所有未被软删除的 Prompt 模板
        base = select(PromptTemplate).where(PromptTemplate.deleted_at.is_(None))
        if role:
            # 步骤 2: 如果传入了角色筛选参数，添加 role 过滤条件
            base = base.where(PromptTemplate.role == role)

        # 步骤 3: 按角色名升序、创建时间降序排序（同角色下最新版本在前）
        base = base.order_by(
            PromptTemplate.role.asc(),
            PromptTemplate.created_at.desc(),
        )

        # 步骤 4: 统计符合条件的总记录数（用于前端分页组件显示总页数）
        count_q = select(func.count()).select_from(base.subquery())
        total = (await db.execute(count_q)).scalar() or 0

        # 步骤 5: 计算偏移量，执行分页查询获取当前页数据
        offset = (page - 1) * page_size
        rows = (
            await db.execute(base.offset(offset).limit(page_size))
        ).scalars().all()

        # 步骤 6: 将数据库模型对象转换为 API 响应模型列表
        items = [
            PromptListItem(
                id=r.id,
                role=r.role,
                version=r.version,
                is_active=r.is_active,
                description=r.description,
                updated_by=r.updated_by,
                created_at=_iso(r.created_at),
            )
            for r in rows
        ]

        # 步骤 7: 返回标准分页响应
        return PaginatedResponse(
            items=items, total=total, page=page, page_size=page_size,
        )


@router.get("/{prompt_id}")
async def get_prompt(prompt_id: int) -> PromptDetail:
    """获取单个 Prompt 模板的完整详情，包括完整文本内容。

    已软删除（deleted_at 非空）的记录会返回 404，对外表现为不存在。

    参数:
        prompt_id: Prompt 模板的数据库主键 ID。

    返回:
        包含完整 content 和所有元数据的 PromptDetail 响应对象。

    异常:
        HTTPException(404): 当指定 ID 的 Prompt 不存在或已被软删除时抛出。
    """
    async with get_db() as db:
        # 步骤 1: 按主键查询，同时排除已软删除的记录
        prompt = (
            await db.execute(
                select(PromptTemplate).where(
                    PromptTemplate.id == prompt_id,
                    PromptTemplate.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()

        # 步骤 2: 未找到则返回 404
        if prompt is None:
            raise HTTPException(status_code=404, detail="Prompt not found")

        # 步骤 3: 将数据库对象转换为详情响应模型（包含完整的 Prompt 文本内容）
        return PromptDetail(
            id=prompt.id,
            role=prompt.role,
            version=prompt.version,
            content=prompt.content,
            is_active=prompt.is_active,
            description=prompt.description,
            updated_by=prompt.updated_by,
            created_at=_iso(prompt.created_at),
        )


@router.post("", status_code=201)
async def create_prompt(body: PromptCreate) -> PromptDetail:
    """新增 Prompt 模板版本，返回 HTTP 201（已创建）。

    新创建的版本默认 is_active=False（未激活），需要通过激活接口单独设为激活。
    系统会校验 role 字段是否在预定义的合法角色集合中。

    参数:
        body: PromptCreate 请求体，包含 role、version、content 等必填字段。

    返回:
        新创建的 PromptDetail 对象，包含数据库生成的 id 和创建时间。

    异常:
        HTTPException(400): 当 role 不在 _KNOWN_PROMPT_ROLES 预定义集合中时抛出。
    """
    # 步骤 1: 校验 role 是否合法 — 防止创建未知角色的 Prompt
    if body.role not in _KNOWN_PROMPT_ROLES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown role: '{body.role}'. Allowed: {', '.join(sorted(_KNOWN_PROMPT_ROLES))}",
        )

    async with get_db() as db:
        # 步骤 2: 构建 PromptTemplate 数据库模型对象
        # is_active 初始为 False，需要管理员手动激活
        prompt = PromptTemplate(
            role=body.role,
            version=body.version,
            content=body.content,
            description=body.description,
            updated_by=body.updated_by,
            is_active=False,
            created_at=datetime.now(timezone.utc),
        )
        # 步骤 3: 加入会话并提交到数据库
        db.add(prompt)
        await db.commit()
        # 步骤 4: 刷新以获取数据库自动生成的字段（如 id）
        await db.refresh(prompt)

        # 步骤 5: 将数据库对象转换为 API 响应并返回
        return PromptDetail(
            id=prompt.id,
            role=prompt.role,
            version=prompt.version,
            content=prompt.content,
            is_active=prompt.is_active,
            description=prompt.description,
            updated_by=prompt.updated_by,
            created_at=_iso(prompt.created_at),
        )


@router.put("/{prompt_id}/activate")
async def activate_prompt(prompt_id: int) -> dict:
    """激活指定的 Prompt 版本，同时停用同一角色下的所有其他激活版本。

    该操作保证同一角色（role）在同一时刻只有一个版本处于激活状态。
    只操作未被软删除的记录。

    参数:
        prompt_id: 要激活的 Prompt 模板的数据库主键 ID。

    返回:
        包含 success 状态和激活版本标识字符串的字典。

    异常:
        HTTPException(404): 当指定 ID 的 Prompt 不存在时抛出。
    """
    async with get_db() as db:
        # 步骤 1: 通过主键查找目标 Prompt
        target = await db.get(PromptTemplate, prompt_id)
        if target is None:
            raise HTTPException(status_code=404, detail="Prompt not found")

        # 步骤 2: 查找同角色下所有当前已激活且未软删除的版本，全部设为停用
        others = (
            await db.execute(
                select(PromptTemplate)
                .where(PromptTemplate.role == target.role)
                .where(PromptTemplate.is_active == True)
                .where(PromptTemplate.deleted_at.is_(None))
            )
        ).scalars().all()
        for p in others:
            p.is_active = False

        # 步骤 3: 将目标版本设为激活状态
        target.is_active = True
        # 步骤 4: 提交事务（停用旧版本 + 激活新版本在同一个事务中完成）
        await db.commit()

        # 步骤 5: 返回成功响应，包含激活的版本标识信息
        return {"success": True, "activated": f"{target.role} v{target.version}"}


@router.delete("/{prompt_id}")
async def delete_prompt(prompt_id: int):
    """软删除指定的 Prompt 版本（设置 deleted_at 时间戳，不清除数据）。

    使用软删除策略而非物理删除：
    - 将 deleted_at 字段设置为当前 UTC 时间
    - 同时将 is_active 设为 False（已被删除不能保持激活）
    - 数据库中保留记录，方便后续审计和数据恢复

    参数:
        prompt_id: 要删除的 Prompt 模板的数据库主键 ID。

    返回:
        包含 success 状态、删除信息、被删 ID 和删除时间的字典。
        返回 HTTP 200（而非物理删除惯用的 204），明确告知客户端软删除已生效。

    异常:
        HTTPException(404): 当指定 ID 的 Prompt 不存在或已被软删除时抛出。
    """
    async with get_db() as db:
        # 步骤 1: 按主键查询，仅查找未被软删除的记录（防止重复删除）
        prompt = (
            await db.execute(
                select(PromptTemplate).where(
                    PromptTemplate.id == prompt_id,
                    PromptTemplate.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()

        # 步骤 2: 未找到则返回 404
        if prompt is None:
            raise HTTPException(status_code=404, detail="Prompt not found")

        # 步骤 3: 执行软删除 — 设置删除时间戳
        prompt.deleted_at = datetime.now(timezone.utc)
        # 步骤 4: 已被删除的版本不应保持激活状态
        prompt.is_active = False
        # 步骤 5: 提交事务
        await db.commit()

        # 步骤 6: 返回包含删除详情的成功响应
        return {
            "success": True,
            "message": f"Prompt '{prompt.role} v{prompt.version}' soft-deleted",
            "id": prompt_id,
            "deleted_at": _iso(prompt.deleted_at),
        }
