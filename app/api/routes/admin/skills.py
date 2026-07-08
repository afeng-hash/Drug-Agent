"""
Admin Skill 管理中心 + SOP 编排。

本模块是后台管理系统中"技能（Skill）"资源的核心控制器，负责技能的完整生命周期管理，
包括技能的增删改查、版本管理、版本激活，以及 SOP（标准操作流程）的编排与校验。

主要功能分为两大块：
1. 技能 CRUD + 版本管理：
   - 技能列表、详情、创建
   - 版本发布与激活（激活后将版本号写入 skill.current_version）
   - 模拟执行（预留接口，待接入 SOPEngine）

2. SOP 编排（每个技能版本内维护一个有序步骤列表）：
   - 查看 / 整体替换 SOP
   - 添加 / 编辑 / 删除单个步骤
   - 校验 SOP：检查工具名有效性、参数模板占位符格式、并行组冲突

API 端点一览:
GET    /api/v1/admin/skills                         — 技能列表（分页）
GET    /api/v1/admin/skills/{id}                     — 技能详情 + 所有版本历史
POST   /api/v1/admin/skills                          — 创建技能
POST   /api/v1/admin/skills/{id}/versions            — 发布新版本
PUT    /api/v1/admin/skills/{id}/versions/{vid}/activate — 激活版本
POST   /api/v1/admin/skills/{id}/test                — 模拟执行

SOP 编排:
GET    /api/v1/admin/skills/{id}/versions/{vid}/sop  — 查看 SOP
PUT    /api/v1/admin/skills/{id}/versions/{vid}/sop  — 替换 SOP
POST   /api/v1/admin/skills/{id}/versions/{vid}/steps — 添加步骤
PUT    /api/v1/admin/skills/{id}/versions/{vid}/steps/{order} — 编辑步骤
DELETE /api/v1/admin/skills/{id}/versions/{vid}/steps/{order} — 删除步骤
POST   /api/v1/admin/skills/{id}/versions/{vid}/validate — 校验 SOP
"""

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.routes.admin.schemas import PaginatedResponse
from app.db.database import get_db
from app.db.models import Skill, SkillVersion

# 创建 FastAPI 路由器，所有路由前缀为 /skills，归属 admin 标签组
router = APIRouter(prefix="/skills", tags=["admin"])

# ── 已知的 task_type 枚举值 ──
# 定义系统中合法的技能分类类型，用 frozenset 保证不可变性
# 这些类型对应不同的药学咨询领域
_VALID_TASK_TYPES = frozenset({
    "side_effects",          # 副作用查询
    "contraindications",     # 禁忌症查询
    "dosage",                # 用法用量查询
    "efficacy",              # 疗效查询
    "drug_interaction",      # 药物相互作用
    "drug_comparison",       # 药物对比
    "special_population",    # 特殊人群用药
    "general_consultation",  # 通用咨询
})


def _iso(ts: datetime | None) -> str | None:
    """将 datetime 对象转换为 ISO 8601 格式字符串。

    参数:
        ts: 一个 datetime 对象，可以为 None。

    返回:
        如果 ts 不为 None，返回其 ISO 8601 字符串表示；否则返回 None。
        用于将数据库中的时间字段统一转换为 API 响应中的字符串格式。
    """
    return ts.isoformat() if ts else None


# ── Schema ──────────────────────────────────────────────────
# 以下 Pydantic 模型定义了 API 的请求体和响应体结构


class SkillListItem(BaseModel):
    """技能列表项的响应模型，用于分页查询接口的返回数据。

    字段:
        id: 技能唯一标识 ID。
        name: 技能名称。
        task_type: 技能对应的任务类型（如 dosage、side_effects 等）。
        status: 技能状态（draft / active / inactive 等）。
        current_version: 当前激活的版本号，若未激活任何版本则为 None。
        description: 技能的描述信息。
        created_at: 技能创建时间（ISO 8601 字符串），可能为 None。
    """
    id: int
    name: str
    task_type: str
    status: str
    current_version: str | None
    description: str
    created_at: str | None


class SkillCreate(BaseModel):
    """创建新技能的请求模型。

    字段:
        name: 技能名称，必填。
        task_type: 技能对应的任务类型，必须在 _VALID_TASK_TYPES 集合中。
        description: 技能的描述信息，默认为空字符串。
    """
    name: str
    task_type: str
    description: str = ""


class SkillVersionCreate(BaseModel):
    """创建技能新版本的请求模型。

    字段:
        version: 版本号字符串（如 "1.0.0"）。
        sop_steps: SOP 步骤列表，每项为一个步骤字典，默认为空列表。
        response_structure: 响应结构模板，默认为空字符串。
        mandatory_reminders: 必须强制的提醒事项列表，默认为空列表。
        fallback_response: 兜底回复文本，当 SOP 执行失败时使用，默认为空字符串。
        changelog: 版本变更日志，默认为空字符串。
        created_by: 创建者标识，默认为 "system"。
    """
    version: str
    sop_steps: list[dict] = Field(default_factory=list)
    response_structure: str = ""
    mandatory_reminders: list[str] = Field(default_factory=list)
    fallback_response: str = ""
    changelog: str = ""
    created_by: str = "system"


class SkillVersionOut(BaseModel):
    """技能版本的输出（响应）模型。

    字段:
        id: 版本记录的唯一标识 ID。
        skill_id: 所属技能的 ID。
        version: 版本号字符串。
        sop_steps: SOP 步骤列表。
        response_structure: 响应结构模板。
        mandatory_reminders: 强制提醒事项列表。
        fallback_response: 兜底回复文本。
        changelog: 版本变更日志。
        created_by: 创建者标识。
        created_at: 版本创建时间（ISO 8601 字符串），可能为 None。
    """
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
    """SOP 单个步骤的输入模型，用于添加或编辑步骤。

    字段:
        order: 步骤的执行顺序号，数字越小越先执行。
        tool_name: 该步骤调用的工具名称。
        args_template: 工具调用的参数模板（字典），默认为空字典。
        parallel_group: 并行组编号，同组步骤可并行执行，0 表示不参与并行，默认为 0。
        is_critical: 是否为关键步骤，关键步骤失败则终止执行，默认为 True。
        timeout_ms: 步骤超时时间（毫秒），None 表示无超时限制，默认为 None。
    """
    order: int
    tool_name: str
    args_template: dict = Field(default_factory=dict)
    parallel_group: int = 0
    is_critical: bool = True
    timeout_ms: int | None = None


class SOPValidateResult(BaseModel):
    """SOP 校验结果的响应模型。

    字段:
        valid: 校验是否通过，True 表示 SOP 合法。
        errors: 错误信息列表，valid 为 True 时通常为空。
    """
    valid: bool
    errors: list[str] = Field(default_factory=list)


# ── Skills CRUD ───────────────────────────────────────────
# 以下端点实现技能的增删改查基本操作


@router.get("")
async def list_skills(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    status: str | None = Query(default=None),
) -> PaginatedResponse[SkillListItem]:
    """分页查询技能列表。

    这一步的作用：从数据库分页获取所有技能记录，支持按状态筛选，返回统一的分页响应。
    可用于后台管理界面的技能列表展示。

    参数:
        request: FastAPI Request 对象，用于注入请求上下文。
        page: 当前页码，从 1 开始，默认 1。
        page_size: 每页记录数，范围 1-100，默认 20。
        status: 可选的技能状态筛选条件（如 "active"、"draft"），为 None 时查询全部。

    返回:
        PaginatedResponse[SkillListItem]: 包含分页信息（items、total、page、page_size）的响应对象。
    """
    async with get_db() as db:
        # 构建基础查询：SELECT * FROM skills
        base = select(Skill)
        # 如果指定了状态筛选条件，追加 WHERE 子句
        if status:
            base = base.where(Skill.status == status)
        # 按技能名称升序排列，保证分页结果的稳定性
        base = base.order_by(Skill.name.asc())

        # 子查询统计总记录数
        count_q = select(func.count()).select_from(base.subquery())
        total = (await db.execute(count_q)).scalar() or 0

        # 计算偏移量，用于分页
        offset = (page - 1) * page_size
        rows = (
            await db.execute(base.offset(offset).limit(page_size))
        ).scalars().all()

        # 将 ORM 对象转换为 SkillListItem 响应模型
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
    """获取技能详情 + 所有版本历史。

    这一步的作用：根据技能 ID 查询单个技能的完整信息，包括其下所有版本记录（按创建时间倒序）。
    用于后台管理中的技能详情页和版本历史查看。

    参数:
        skill_id: 技能的数据库主键 ID。

    返回:
        dict: 包含技能基本信息以及 versions 列表的字典。若技能不存在则抛出 404 错误。
    """
    async with get_db() as db:
        # 通过主键查询技能记录
        skill = await db.get(Skill, skill_id)
        if skill is None:
            raise HTTPException(status_code=404, detail="Skill not found")

        # 查询该技能的所有版本，按创建时间倒序排列（最新的在前）
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
    """创建新技能。

    这一步的作用：接收技能创建请求，校验 task_type 的合法性及唯一性，然后将新技能写入数据库。
    新创建的技能状态默认为 "draft"（草稿）。

    参数:
        body: SkillCreate 请求体，包含 name、task_type、description 字段。

    返回:
        SkillListItem: 新创建的技能信息。若 task_type 不合法返回 400，若已存在同名 task_type 则返回 409。
    """
    # 校验 task_type 是否在合法枚举值集合中
    if body.task_type not in _VALID_TASK_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown task_type: '{body.task_type}'. "
                   f"Allowed: {', '.join(sorted(_VALID_TASK_TYPES))}",
        )
    async with get_db() as db:
        # 检查 task_type 唯一性（数据库层有 unique 约束，这里提前友好报错）
        # 每个 task_type 只能对应一个技能定义
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

        # 创建 Skill ORM 对象，初始状态设为草稿
        skill = Skill(
            name=body.name,
            task_type=body.task_type,
            description=body.description,
            status="draft",
        )
        db.add(skill)
        await db.commit()
        # 刷新以获取数据库生成的 id 和 created_at 等字段
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
# 以下端点实现技能版本的创建、激活和模拟测试


@router.post("/{skill_id}/versions", status_code=201)
async def create_skill_version(
    skill_id: int, body: SkillVersionCreate
) -> SkillVersionOut:
    """为技能发布新版本。

    这一步的作用：在指定技能下创建一个新的版本记录（SkillVersion），包含 SOP 步骤、响应结构、
    强制提醒和兜底回复等完整版本定义。注意：创建版本后不会自动激活，需要调用 activate 接口。

    参数:
        skill_id: 目标技能的数据库主键 ID。
        body: SkillVersionCreate 请求体，包含 version、sop_steps、response_structure 等字段。

    返回:
        SkillVersionOut: 新创建的版本信息。若技能不存在则返回 404 错误。
    """
    async with get_db() as db:
        # 先校验目标技能是否存在
        skill = await db.get(Skill, skill_id)
        if skill is None:
            raise HTTPException(status_code=404, detail="Skill not found")

        # 创建 SkillVersion ORM 对象
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
        # 刷新以获取数据库生成的自增 id 和 created_at 等字段
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
    """激活技能版本。

    这一步的作用：将指定版本设置为技能的当前活跃版本，同时更新技能状态为 "active" 并记录更新时间。
    激活后的版本将被系统实际使用（如 SOPEngine 执行时会读取 current_version 指向的版本）。

    参数:
        skill_id: 目标技能的数据库主键 ID。
        version_id: 要激活的版本记录的数据库主键 ID。

    返回:
        dict: 包含 success=True 和 activated 字符串（格式："技能名 v版本号"）的字典。
              若技能或版本不存在则返回 404 错误。
    """
    async with get_db() as db:
        # 查找目标技能
        skill = await db.get(Skill, skill_id)
        if skill is None:
            raise HTTPException(status_code=404, detail="Skill not found")

        # 查找目标版本，同时校验版本所属技能是否匹配
        sv = await db.get(SkillVersion, version_id)
        if sv is None or sv.skill_id != skill_id:
            raise HTTPException(status_code=404, detail="Version not found")

        # 将技能的当前版本指向该版本号，更新状态和时间戳
        skill.current_version = sv.version
        skill.updated_at = datetime.now(timezone.utc)
        skill.status = "active"
        await db.commit()

        return {"success": True, "activated": f"{skill.name} v{sv.version}"}


@router.post("/{skill_id}/test")
async def test_skill(skill_id: int) -> dict:
    """模拟执行技能（待接入 SOPEngine）。

    这一步的作用：对指定技能进行模拟执行测试，验证 SOP 定义是否能正常运行。
    当前为预留接口，尚未实现具体逻辑，待后续接入 SOPEngine 后完成。

    参数:
        skill_id: 目标技能的数据库主键 ID。

    返回:
        dict: 当前直接返回 501 Not Implemented 错误，提示需要 SOPEngine 集成。
    """
    async with get_db() as db:
        skill = await db.get(Skill, skill_id)
        if skill is None:
            raise HTTPException(status_code=404, detail="Skill not found")
        raise HTTPException(
            status_code=501,
            detail="Skill test execution not yet implemented (requires SOPEngine integration).",
        )


# ── SOP 编排 ─────────────────────────────────────────────
# 以下端点实现 SOP（标准操作流程）的细粒度编排操作，
# 包括查看、整体替换、单步增删改以及校验


@router.get("/{skill_id}/versions/{version_id}/sop")
async def get_sop(skill_id: int, version_id: int) -> dict:
    """查看指定版本的 SOP 定义。

    这一步的作用：获取某个技能版本的完整 SOP 编排信息，包括步骤列表、响应结构、强制提醒和兜底回复。
    用于后台管理中的 SOP 预览和编辑前的数据加载。

    参数:
        skill_id: 技能的数据库主键 ID。
        version_id: 版本记录的数据库主键 ID。

    返回:
        dict: 包含 steps、response_structure、mandatory_reminders、fallback_response 的字典。
              若版本不存在或不属于指定技能则返回 404 错误。
    """
    async with get_db() as db:
        # 查询目标版本并校验所属技能
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
    """整体替换 SOP 定义。

    这一步的作用：一次性替换指定版本的完整 SOP 配置（步骤、响应结构、强制提醒、兜底回复）。
    请求体中未提供的字段将保持原值不变。适用于批量编辑场景。

    参数:
        skill_id: 技能的数据库主键 ID。
        version_id: 版本记录的数据库主键 ID。
        body: 请求体字典，可包含 steps、response_structure、mandatory_reminders、fallback_response 任一字段。

    返回:
        dict: 更新后的完整 SOP 配置。若版本不存在或不属于指定技能则返回 404 错误。
    """
    async with get_db() as db:
        # 查询目标版本并校验所属技能
        sv = await db.get(SkillVersion, version_id)
        if sv is None or sv.skill_id != skill_id:
            raise HTTPException(status_code=404, detail="Version not found")

        # 逐一替换字段：请求体中有值的字段则更新，否则保持原值
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
    """添加 SOP 步骤。

    这一步的作用：向指定版本的 SOP 步骤列表末尾追加一个新步骤。
    用于在已有 SOP 编排中逐步添加新的执行步骤。

    参数:
        skill_id: 技能的数据库主键 ID。
        version_id: 版本记录的数据库主键 ID。
        body: SOPStepIn 请求体，包含 order、tool_name、args_template 等字段。

    返回:
        dict: 包含更新后的完整 steps 列表的字典。若版本不存在或不属于指定技能则返回 404 错误。
    """
    async with get_db() as db:
        # 查询目标版本并校验所属技能
        sv = await db.get(SkillVersion, version_id)
        if sv is None or sv.skill_id != skill_id:
            raise HTTPException(status_code=404, detail="Version not found")

        # 将 Pydantic 模型转为字典，追加到现有步骤列表末尾
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
    """编辑 SOP 步骤。

    这一步的作用：根据 order 字段定位 SOP 中的某个步骤，然后将其整体替换为请求体中的新内容。
    Order 是步骤的唯一标识（而非数组索引），因此即使步骤列表顺序变化也能准确定位。

    参数:
        skill_id: 技能的数据库主键 ID。
        version_id: 版本记录的数据库主键 ID。
        order: 要编辑的步骤的 order 值，用于在步骤列表中定位。
        body: SOPStepIn 请求体，包含更新后的步骤完整字段。

    返回:
        dict: 包含更新后的完整 steps 列表的字典。
              若版本不存在或不属于指定技能则返回 404 错误，
              若找不到匹配 order 的步骤也返回 404 错误。
    """
    async with get_db() as db:
        # 查询目标版本并校验所属技能
        sv = await db.get(SkillVersion, version_id)
        if sv is None or sv.skill_id != skill_id:
            raise HTTPException(status_code=404, detail="Version not found")

        # 在步骤列表中查找 order 匹配的步骤索引
        steps = list(sv.sop_steps)
        idx = None
        for i, s in enumerate(steps):
            if s.get("order") == order:
                idx = i
                break
        if idx is None:
            raise HTTPException(status_code=404, detail=f"Step order={order} not found")

        # 将匹配到的步骤替换为新的内容
        steps[idx] = body.model_dump()
        sv.sop_steps = steps
        await db.commit()

        return {"steps": sv.sop_steps}


@router.delete("/{skill_id}/versions/{version_id}/steps/{order}")
async def delete_sop_step(skill_id: int, version_id: int, order: int) -> dict:
    """删除 SOP 步骤。

    这一步的作用：从指定版本的 SOP 步骤列表中移除 order 匹配的步骤。
    使用列表推导式过滤掉匹配项，不会影响其他步骤的顺序。

    参数:
        skill_id: 技能的数据库主键 ID。
        version_id: 版本记录的数据库主键 ID。
        order: 要删除的步骤的 order 值。

    返回:
        dict: 包含删除后完整 steps 列表的字典。若版本不存在或不属于指定技能则返回 404 错误。
    """
    async with get_db() as db:
        # 查询目标版本并校验所属技能
        sv = await db.get(SkillVersion, version_id)
        if sv is None or sv.skill_id != skill_id:
            raise HTTPException(status_code=404, detail="Version not found")

        # 过滤掉 order 匹配的步骤，保留其余所有步骤
        steps = [s for s in sv.sop_steps if s.get("order") != order]
        sv.sop_steps = steps
        await db.commit()

        return {"steps": sv.sop_steps}


@router.post("/{skill_id}/versions/{version_id}/validate")
async def validate_sop(
    skill_id: int, version_id: int, request: Request
) -> SOPValidateResult:
    """校验 SOP — 工具名有效性、参数模板合法性、并行组冲突检查。

    这一步的作用：对指定版本的 SOP 定义进行完整性校验，确保所有步骤引用的工具已注册且为 active 状态，
    参数模板中的占位符格式正确，且同并行组内没有 order 冲突。校验结果用于在发布或激活前确认 SOP 的合法性。

    参数:
        skill_id: 技能的数据库主键 ID。
        version_id: 版本记录的数据库主键 ID。
        request: FastAPI Request 对象，用于注入请求上下文。

    返回:
        SOPValidateResult: 包含 valid（布尔值）和 errors（错误信息列表）的校验结果对象。
                           若版本不存在或不属于指定技能则返回 404 错误。
    """
    async with get_db() as db:
        # 查询目标版本并校验所属技能
        sv = await db.get(SkillVersion, version_id)
        if sv is None or sv.skill_id != skill_id:
            raise HTTPException(status_code=404, detail="Version not found")

        from app.db.models import Tool as ToolModel

        # 获取所有 active 状态的工具名，用于后续校验工具名有效性
        tool_rows = (
            await db.execute(
                select(ToolModel.name).where(ToolModel.status == "active")
            )
        ).all()
        valid_tools = {r[0] for r in tool_rows}

        errors = []
        steps = sv.sop_steps

        # 遍历每个步骤，逐项校验
        for s in steps:
            # 校验一：工具名是否在已注册的 active 工具列表中
            tool = s.get("tool_name", "")
            if tool not in valid_tools:
                errors.append(f"Step {s.get('order')}: tool '{tool}' is not registered or inactive")
            # 校验二：参数模板中的占位符格式是否合法（{xxx} 形式）
            args = s.get("args_template", {})
            for k, v in args.items():
                if isinstance(v, str) and "{" in v and "}" in v:
                    # 使用正则提取占位符，检查格式是否为 {word}
                    import re
                    placeholders = re.findall(r"\{(\w+)\}", v)
                    if not placeholders:
                        errors.append(f"Step {s.get('order')}: invalid placeholder in args_template.{k}: {v}")

        # 校验三：并行组冲突检查 — 同 parallel_group > 0 的步骤必须具有相同的 order 值
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
