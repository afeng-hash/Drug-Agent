"""
Admin Prompt 管理中心 — 版本管理与 CRUD。

GET    /api/v1/admin/prompts             — 列表
GET    /api/v1/admin/prompts/{id}        — 详情
POST   /api/v1/admin/prompts             — 新增版本
PUT    /api/v1/admin/prompts/{id}/activate — 激活版本
DELETE /api/v1/admin/prompts/{id}        — 删除

Phase 1: 仅管理界面 CRUD，运行时不从 DB 读 Prompt（仍用代码常量）。
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
    return ts.isoformat() if ts else None


# ── Schema ──────────────────────────────────────────────────


class PromptListItem(BaseModel):
    id: int
    role: str
    version: str
    is_active: bool
    description: str
    updated_by: str
    created_at: str | None


_KNOWN_PROMPT_ROLES = frozenset({
    "dispatcher", "consult", "react", "recommend",
    "classifier", "generator", "safety_block",
})


class PromptCreate(BaseModel):
    role: str = Field(..., description="Prompt 角色名", min_length=1, max_length=50)
    version: str = Field(..., description="语义版本号", min_length=1, max_length=20)
    content: str = Field(..., description="Prompt 全文", min_length=1)
    description: str = Field(default="", max_length=500)
    updated_by: str = Field(default="system", max_length=100)


class PromptDetail(BaseModel):
    id: int
    role: str
    version: str
    content: str
    is_active: bool
    description: str
    updated_by: str
    created_at: str | None


# ── Routes ──────────────────────────────────────────────────


@router.get("")
async def list_prompts(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    role: str | None = Query(default=None, description="按角色筛选"),
) -> PaginatedResponse[PromptListItem]:
    """分页查询 Prompt 模板列表。"""
    async with get_db() as db:
        base = select(PromptTemplate).where(PromptTemplate.deleted_at.is_(None))
        if role:
            base = base.where(PromptTemplate.role == role)

        base = base.order_by(
            PromptTemplate.role.asc(),
            PromptTemplate.created_at.desc(),
        )

        count_q = select(func.count()).select_from(base.subquery())
        total = (await db.execute(count_q)).scalar() or 0

        offset = (page - 1) * page_size
        rows = (
            await db.execute(base.offset(offset).limit(page_size))
        ).scalars().all()

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

        return PaginatedResponse(
            items=items, total=total, page=page, page_size=page_size,
        )


@router.get("/{prompt_id}")
async def get_prompt(prompt_id: int) -> PromptDetail:
    """获取单个 Prompt 模板详情（已软删除的返回 404）。"""
    async with get_db() as db:
        prompt = (
            await db.execute(
                select(PromptTemplate).where(
                    PromptTemplate.id == prompt_id,
                    PromptTemplate.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if prompt is None:
            raise HTTPException(status_code=404, detail="Prompt not found")

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
    """新增 Prompt 版本。"""
    if body.role not in _KNOWN_PROMPT_ROLES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown role: '{body.role}'. Allowed: {', '.join(sorted(_KNOWN_PROMPT_ROLES))}",
        )
    async with get_db() as db:
        prompt = PromptTemplate(
            role=body.role,
            version=body.version,
            content=body.content,
            description=body.description,
            updated_by=body.updated_by,
            is_active=False,
            created_at=datetime.now(timezone.utc),
        )
        db.add(prompt)
        await db.commit()
        await db.refresh(prompt)

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
    """激活指定版本（停用同 role 的所有其他版本）。"""
    async with get_db() as db:
        target = await db.get(PromptTemplate, prompt_id)
        if target is None:
            raise HTTPException(status_code=404, detail="Prompt not found")

        # 停用同 role 的所有版本（排除已软删除的）
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

        # 激活目标
        target.is_active = True
        await db.commit()

        return {"success": True, "activated": f"{target.role} v{target.version}"}


@router.delete("/{prompt_id}")
async def delete_prompt(prompt_id: int):
    """软删除 Prompt 版本（设置 deleted_at 时间戳）。

    Returns:
        200 + success body（与物理删除的 204 区分，明确表示软删除已生效）。
    """
    async with get_db() as db:
        prompt = (
            await db.execute(
                select(PromptTemplate).where(
                    PromptTemplate.id == prompt_id,
                    PromptTemplate.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if prompt is None:
            raise HTTPException(status_code=404, detail="Prompt not found")
        # 软删除
        prompt.deleted_at = datetime.now(timezone.utc)
        prompt.is_active = False
        await db.commit()
        return {
            "success": True,
            "message": f"Prompt '{prompt.role} v{prompt.version}' soft-deleted",
            "id": prompt_id,
            "deleted_at": _iso(prompt.deleted_at),
        }
