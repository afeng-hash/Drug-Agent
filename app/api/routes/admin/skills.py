"""
Admin Skill 管理中心 + SOP 编排。

GET    /api/v1/admin/skills                         — 技能列表
GET    /api/v1/admin/skills/{id}                     — 技能详情
POST   /api/v1/admin/skills                          — 创建技能
POST   /api/v1/admin/skills/{id}/versions            — 发布新版本
PUT    /api/v1/admin/skills/{id}/versions/{vid}/activate — 激活版本
POST   /api/v1/admin/skills/{id}/test                — 模拟执行

SOP 编排:
GET    /api/v1/admin/skills/{id}/versions/{vid}/sop  — 查看 SOP
PUT    /api/v1/admin/skills/{id}/versions/{vid}/sop  — 替换 SOP
POST   /api/v1/admin/skills/{id}/versions/{vid}/steps — 添加步骤
PUT    /api/v1/admin/skills/{id}/versions/{vid}/steps/{order} — 编辑步骤
DELETE /api/v1/admin/skills/{id}/versions/{vid}/steps/{order}
POST   /api/v1/admin/skills/{id}/versions/{vid}/validate — 校验 SOP
"""

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.routes.admin.schemas import PaginatedResponse
from app.db.database import get_db
from app.db.models import Skill, SkillVersion

router = APIRouter(prefix="/skills", tags=["admin"])

# ── 已知的 task_type 枚举值 ──
_VALID_TASK_TYPES = frozenset({
    "side_effects", "contraindications", "dosage", "efficacy",
    "drug_interaction", "drug_comparison", "special_population",
    "general_consultation",
})


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts else None


# ── Schema ──────────────────────────────────────────────────


class SkillListItem(BaseModel):
    id: int
    name: str
    task_type: str
    status: str
    current_version: str | None
    description: str
    created_at: str | None


class SkillCreate(BaseModel):
    name: str
    task_type: str
    description: str = ""


class SkillVersionCreate(BaseModel):
    version: str
    sop_steps: list[dict] = Field(default_factory=list)
    response_structure: str = ""
    mandatory_reminders: list[str] = Field(default_factory=list)
    fallback_response: str = ""
    changelog: str = ""
    created_by: str = "system"


class SkillVersionOut(BaseModel):
    id: int
    skill_id: int
    version: str
    sop_steps: list[dict]
    response_structure: str
    mandatory_reminders: list[str]
    fallback_response: str
    changelog: str
    created_by: str
    created_at: str | None


class SOPStepIn(BaseModel):
    order: int
    tool_name: str
    args_template: dict = Field(default_factory=dict)
    parallel_group: int = 0
    is_critical: bool = True
    timeout_ms: int | None = None


class SOPValidateResult(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)


# ── Skills CRUD ───────────────────────────────────────────


@router.get("")
async def list_skills(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    status: str | None = Query(default=None),
) -> PaginatedResponse[SkillListItem]:
    """分页查询技能列表。"""
    async with get_db() as db:
        base = select(Skill)
        if status:
            base = base.where(Skill.status == status)
        base = base.order_by(Skill.name.asc())

        count_q = select(func.count()).select_from(base.subquery())
        total = (await db.execute(count_q)).scalar() or 0

        offset = (page - 1) * page_size
        rows = (
            await db.execute(base.offset(offset).limit(page_size))
        ).scalars().all()

        items = [
            SkillListItem(
                id=r.id,
                name=r.name,
                task_type=r.task_type,
                status=r.status,
                current_version=r.current_version,
                description=r.description,
                created_at=_iso(r.created_at),
            )
            for r in rows
        ]

        return PaginatedResponse(
            items=items, total=total, page=page, page_size=page_size,
        )


@router.get("/{skill_id}")
async def get_skill(skill_id: int) -> dict:
    """获取技能详情 + 所有版本历史。"""
    async with get_db() as db:
        skill = await db.get(Skill, skill_id)
        if skill is None:
            raise HTTPException(status_code=404, detail="Skill not found")

        versions = (
            await db.execute(
                select(SkillVersion)
                .where(SkillVersion.skill_id == skill_id)
                .order_by(SkillVersion.created_at.desc())
            )
        ).scalars().all()

        return {
            "id": skill.id,
            "name": skill.name,
            "task_type": skill.task_type,
            "status": skill.status,
            "current_version": skill.current_version,
            "description": skill.description,
            "created_at": _iso(skill.created_at),
            "updated_at": _iso(skill.updated_at),
            "versions": [
                {
                    "id": v.id,
                    "version": v.version,
                    "sop_steps": v.sop_steps,
                    "response_structure": v.response_structure,
                    "mandatory_reminders": v.mandatory_reminders,
                    "fallback_response": v.fallback_response,
                    "changelog": v.changelog,
                    "created_by": v.created_by,
                    "created_at": _iso(v.created_at),
                }
                for v in versions
            ],
        }


@router.post("", status_code=201)
async def create_skill(body: SkillCreate) -> SkillListItem:
    """创建新技能。"""
    if body.task_type not in _VALID_TASK_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown task_type: '{body.task_type}'. "
                   f"Allowed: {', '.join(sorted(_VALID_TASK_TYPES))}",
        )
    async with get_db() as db:
        # 检查 task_type 唯一性（数据库层有 unique 约束，这里提前友好报错）
        existing = (
            await db.execute(
                select(Skill).where(Skill.task_type == body.task_type)
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=f"Skill with task_type '{body.task_type}' already exists "
                       f"(id={existing.id}, name='{existing.name}'). "
                       f"Each task_type can only have one skill definition.",
            )

        skill = Skill(
            name=body.name,
            task_type=body.task_type,
            description=body.description,
            status="draft",
        )
        db.add(skill)
        await db.commit()
        await db.refresh(skill)
        return SkillListItem(
            id=skill.id,
            name=skill.name,
            task_type=skill.task_type,
            status=skill.status,
            current_version=skill.current_version,
            description=skill.description,
            created_at=_iso(skill.created_at),
        )


# ── Version Management ────────────────────────────────────


@router.post("/{skill_id}/versions", status_code=201)
async def create_skill_version(
    skill_id: int, body: SkillVersionCreate
) -> SkillVersionOut:
    """为技能发布新版本。"""
    async with get_db() as db:
        skill = await db.get(Skill, skill_id)
        if skill is None:
            raise HTTPException(status_code=404, detail="Skill not found")

        sv = SkillVersion(
            skill_id=skill_id,
            version=body.version,
            sop_steps=body.sop_steps,
            response_structure=body.response_structure,
            mandatory_reminders=body.mandatory_reminders,
            fallback_response=body.fallback_response,
            changelog=body.changelog,
            created_by=body.created_by,
        )
        db.add(sv)
        await db.commit()
        await db.refresh(sv)

        return SkillVersionOut(
            id=sv.id,
            skill_id=sv.skill_id,
            version=sv.version,
            sop_steps=sv.sop_steps,
            response_structure=sv.response_structure,
            mandatory_reminders=sv.mandatory_reminders,
            fallback_response=sv.fallback_response,
            changelog=sv.changelog,
            created_by=sv.created_by,
            created_at=_iso(sv.created_at),
        )


@router.put("/{skill_id}/versions/{version_id}/activate")
async def activate_skill_version(skill_id: int, version_id: int) -> dict:
    """激活技能版本（更新 skill.current_version 指向此版本）。"""
    async with get_db() as db:
        skill = await db.get(Skill, skill_id)
        if skill is None:
            raise HTTPException(status_code=404, detail="Skill not found")

        sv = await db.get(SkillVersion, version_id)
        if sv is None or sv.skill_id != skill_id:
            raise HTTPException(status_code=404, detail="Version not found")

        skill.current_version = sv.version
        skill.updated_at = datetime.now(timezone.utc)
        skill.status = "active"
        await db.commit()

        return {"success": True, "activated": f"{skill.name} v{sv.version}"}


@router.post("/{skill_id}/test")
async def test_skill(skill_id: int) -> dict:
    """模拟执行技能（待接入 SOPEngine）。"""
    async with get_db() as db:
        skill = await db.get(Skill, skill_id)
        if skill is None:
            raise HTTPException(status_code=404, detail="Skill not found")
        raise HTTPException(
            status_code=501,
            detail="Skill test execution not yet implemented (requires SOPEngine integration).",
        )


# ── SOP 编排 ─────────────────────────────────────────────


@router.get("/{skill_id}/versions/{version_id}/sop")
async def get_sop(skill_id: int, version_id: int) -> dict:
    """查看指定版本的 SOP 定义。"""
    async with get_db() as db:
        sv = await db.get(SkillVersion, version_id)
        if sv is None or sv.skill_id != skill_id:
            raise HTTPException(status_code=404, detail="Version not found")

        return {
            "steps": sv.sop_steps,
            "response_structure": sv.response_structure,
            "mandatory_reminders": sv.mandatory_reminders,
            "fallback_response": sv.fallback_response,
        }


@router.put("/{skill_id}/versions/{version_id}/sop")
async def update_sop(skill_id: int, version_id: int, body: dict) -> dict:
    """整体替换 SOP 定义。"""
    async with get_db() as db:
        sv = await db.get(SkillVersion, version_id)
        if sv is None or sv.skill_id != skill_id:
            raise HTTPException(status_code=404, detail="Version not found")

        sv.sop_steps = body.get("steps", sv.sop_steps)
        sv.response_structure = body.get("response_structure", sv.response_structure)
        sv.mandatory_reminders = body.get("mandatory_reminders", sv.mandatory_reminders)
        sv.fallback_response = body.get("fallback_response", sv.fallback_response)

        await db.commit()
        return {
            "steps": sv.sop_steps,
            "response_structure": sv.response_structure,
            "mandatory_reminders": sv.mandatory_reminders,
            "fallback_response": sv.fallback_response,
        }


@router.post("/{skill_id}/versions/{version_id}/steps", status_code=201)
async def add_sop_step(skill_id: int, version_id: int, body: SOPStepIn) -> dict:
    """添加 SOP 步骤。"""
    async with get_db() as db:
        sv = await db.get(SkillVersion, version_id)
        if sv is None or sv.skill_id != skill_id:
            raise HTTPException(status_code=404, detail="Version not found")

        new_step = body.model_dump()
        steps = list(sv.sop_steps)
        steps.append(new_step)
        sv.sop_steps = steps
        await db.commit()

        return {"steps": sv.sop_steps}


@router.put("/{skill_id}/versions/{version_id}/steps/{order}")
async def update_sop_step(
    skill_id: int, version_id: int, order: int, body: SOPStepIn
) -> dict:
    """编辑 SOP 步骤。"""
    async with get_db() as db:
        sv = await db.get(SkillVersion, version_id)
        if sv is None or sv.skill_id != skill_id:
            raise HTTPException(status_code=404, detail="Version not found")

        steps = list(sv.sop_steps)
        idx = None
        for i, s in enumerate(steps):
            if s.get("order") == order:
                idx = i
                break
        if idx is None:
            raise HTTPException(status_code=404, detail=f"Step order={order} not found")

        steps[idx] = body.model_dump()
        sv.sop_steps = steps
        await db.commit()

        return {"steps": sv.sop_steps}


@router.delete("/{skill_id}/versions/{version_id}/steps/{order}")
async def delete_sop_step(skill_id: int, version_id: int, order: int) -> dict:
    """删除 SOP 步骤。"""
    async with get_db() as db:
        sv = await db.get(SkillVersion, version_id)
        if sv is None or sv.skill_id != skill_id:
            raise HTTPException(status_code=404, detail="Version not found")

        steps = [s for s in sv.sop_steps if s.get("order") != order]
        sv.sop_steps = steps
        await db.commit()

        return {"steps": sv.sop_steps}


@router.post("/{skill_id}/versions/{version_id}/validate")
async def validate_sop(
    skill_id: int, version_id: int, request: Request
) -> SOPValidateResult:
    """校验 SOP — 工具名有效性、参数模板合法性、并行组冲突检查。"""
    async with get_db() as db:
        sv = await db.get(SkillVersion, version_id)
        if sv is None or sv.skill_id != skill_id:
            raise HTTPException(status_code=404, detail="Version not found")

        from app.db.models import Tool as ToolModel

        # 获取所有 active 工具名
        tool_rows = (
            await db.execute(
                select(ToolModel.name).where(ToolModel.status == "active")
            )
        ).all()
        valid_tools = {r[0] for r in tool_rows}

        errors = []
        steps = sv.sop_steps

        for s in steps:
            tool = s.get("tool_name", "")
            if tool not in valid_tools:
                errors.append(f"Step {s.get('order')}: tool '{tool}' is not registered or inactive")
            args = s.get("args_template", {})
            for k, v in args.items():
                if isinstance(v, str) and "{" in v and "}" in v:
                    # 检查占位符格式
                    import re
                    placeholders = re.findall(r"\{(\w+)\}", v)
                    if not placeholders:
                        errors.append(f"Step {s.get('order')}: invalid placeholder in args_template.{k}: {v}")

        # 检查并行组冲突（同 group 的步骤不能有不同 order 值）
        groups = {}
        for s in steps:
            g = s.get("parallel_group", 0)
            o = s.get("order", 0)
            if g > 0:
                if g in groups:
                    if groups[g] != o:
                        errors.append(f"Parallel group {g} has conflicting order values: {groups[g]} vs {o}")
                else:
                    groups[g] = o

        return SOPValidateResult(
            valid=len(errors) == 0,
            errors=errors,
        )
