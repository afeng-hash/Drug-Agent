"""
Admin 数据库管理模块 — 药品 / 库存 / 权重配置 CRUD。

本模块提供了后台管理系统中对三大核心数据实体（药品、库存、权重配置）的完整增删改查接口。
所有删除操作均为"软删除"（设置 deleted_at 时间戳），保留数据完整性以便审计和恢复。

API 端点一览：
  GET    /api/v1/admin/database/drugs             — 分页查询药品列表（支持搜索、分类、OTC 类型过滤）
  POST   /api/v1/admin/database/drugs             — 创建新药品
  GET    /api/v1/admin/database/drugs/{drug_id}   — 获取药品详情（含关联库存）
  PUT    /api/v1/admin/database/drugs/{drug_id}   — 更新药品信息
  DELETE /api/v1/admin/database/drugs/{drug_id}   — 软删除药品
  GET    /api/v1/admin/database/inventory         — 分页查询库存列表（支持按药品、可用状态、库存紧张过滤）
  POST   /api/v1/admin/database/inventory         — 新增库存 SKU
  PUT    /api/v1/admin/database/inventory/{inv_id} — 更新库存 SKU
  DELETE /api/v1/admin/database/inventory/{inv_id} — 软删除库存 SKU
  GET    /api/v1/admin/database/weights           — 获取所有权重配置版本
  POST   /api/v1/admin/database/weights           — 创建新权重版本
  PUT    /api/v1/admin/database/weights/{wc_id}/activate — 激活指定权重版本（同时停用其他所有版本）
"""

import csv
import io
import json
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.routes.admin.schemas import PaginatedResponse
from app.db.database import get_db
from app.db.models import Drug, Feedback, Inventory, WeightConfig

# 创建路由实例，所有端点路径均以 /database 为前缀，在 OpenAPI 文档中归类到 "admin" 标签下
router = APIRouter(prefix="/database", tags=["admin"])


# ── 工具函数 ──────────────────────────────────────────────────


def _drug_to_dict(d: Drug) -> dict:
    """将 Drug ORM 对象转换为普通字典，用于 API 响应序列化。

    这一步的作用：SQLAlchemy 的 ORM 对象不能直接通过 FastAPI 的 JSON 响应返回，
    需要先转换为 Python 原生字典。此函数提取药品的所有核心字段，统一输出格式。

    参数:
        d (Drug): SQLAlchemy Drug 模型实例，从数据库中查询得到的药品对象。

    返回:
        dict: 包含药品 id、通用名、商品名列表、分类、有效成分、剂型、规格、
              OTC 类型、适应症摘要、成人/儿童/老年人用法用量等字段的字典。
    """
    # 将 Drug ORM 对象的关键字段映射为字典键值对
    return {
        "id": d.id,                              # 药品唯一标识
        "generic_name": d.generic_name,           # 通用名称（如"布洛芬"）
        "brand_names": d.brand_names,             # 商品名列表（如["芬必得", "美林"]）
        "category": d.category,                   # 药品分类（如"感冒退烧"、"抗生素"）
        "active_ingredients": d.active_ingredients,  # 有效成分列表
        "dosage_form": d.dosage_form,             # 剂型（如"片剂"、"胶囊"、"口服液"）
        "strength": d.strength,                   # 规格/含量（如"0.3g"）
        "otc_type": d.otc_type,                   # OTC 分类（"甲类" / "乙类" / "处方药"）
        "indication_summary": d.indication_summary,  # 适应症简要说明
        "usage_adult": d.usage_adult,             # 成人用法用量
        "usage_child": d.usage_child,             # 儿童用法用量（可为 None）
        "usage_elderly": d.usage_elderly,         # 老年人用法用量（可为 None）
    }


# ── Pydantic 数据模型（请求/响应 Schema）──────────────────────
#
# 以下类定义了 API 请求体和响应体的数据结构。
# Pydantic 自动进行输入校验和序列化，确保数据符合预期格式。
# Create 类用于 POST 请求（创建资源），Update 类用于 PUT 请求（部分更新），
# Item 类用于 GET 响应（返回资源）。Update 类中字段均为 Optional，
# 表示可以只传需要更新的字段（部分更新 / PATCH 语义）。


class DrugCreate(BaseModel):
    """创建药品的请求体 Schema。

    所有必填字段均设置了默认值或校验规则，确保入库数据质量。
    """
    generic_name: str = Field(..., min_length=1, max_length=200)
    # 通用名称 — 必填，长度 1-200 字符
    brand_names: list = Field(default_factory=list)
    # 商品名列表 — 可为空列表，如 ["泰诺", "百服宁"]
    category: str = Field(default="感冒退烧", min_length=1, max_length=50)
    # 药品分类 — 默认为"感冒退烧"
    active_ingredients: list = Field(default_factory=list)
    # 有效成分列表 — 如 ["对乙酰氨基酚"]
    dosage_form: str = Field(default="", max_length=50)
    # 剂型 — 如"片剂"、"颗粒剂"
    strength: str = Field(default="", max_length=100)
    # 规格 — 如"0.5g×12片"
    otc_type: str = Field(default="甲类", max_length=20)
    # OTC 类型 — 默认为"甲类"
    indication_summary: str = Field(default="", max_length=500)
    # 适应症摘要 — 简要描述该药品用于治疗什么症状
    usage_adult: str = Field(default="", max_length=1000)
    # 成人用法用量
    usage_child: str | None = Field(default=None, max_length=1000)
    # 儿童用法用量 — 可选，因为并非所有药品都适用于儿童
    usage_elderly: str | None = Field(default=None, max_length=1000)
    # 老年人用法用量 — 可选，因为并非所有药品都有专门的老年人用药指导


class DrugUpdate(BaseModel):
    """更新药品的请求体 Schema。

    所有字段均为可选（Optional），仅更新请求中提供的字段，
    未提供的字段保持原值不变。
    """
    generic_name: str | None = Field(default=None, min_length=1, max_length=200)
    # 通用名称 — 可选更新
    brand_names: list | None = None
    # 商品名列表 — 可选更新
    category: str | None = Field(default=None, min_length=1, max_length=50)
    # 药品分类 — 可选更新
    active_ingredients: list | None = None
    # 有效成分列表 — 可选更新
    dosage_form: str | None = None
    # 剂型 — 可选更新
    strength: str | None = None
    # 规格 — 可选更新
    otc_type: str | None = None
    # OTC 类型 — 可选更新
    indication_summary: str | None = None
    # 适应症摘要 — 可选更新
    usage_adult: str | None = None
    # 成人用法用量 — 可选更新
    usage_child: str | None = None
    # 儿童用法用量 — 可选更新
    usage_elderly: str | None = None
    # 老年人用法用量 — 可选更新


class InventoryItem(BaseModel):
    """库存 SKU 的响应体 Schema — 用于 API 返回库存数据。"""
    id: int            # 库存记录唯一标识
    drug_id: int       # 关联的药品 ID（外键）
    product_name: str  # 商品名称
    manufacturer: str  # 生产厂家
    specification: str # 规格（如"10ml×6支"）
    stock_quantity: int  # 当前库存数量
    price: float         # 单价
    shelf_location: str  # 货架位置编码
    is_available: bool   # 是否可售（上架/下架状态）


class InventoryCreate(BaseModel):
    """创建库存 SKU 的请求体 Schema。"""
    drug_id: int
    # 关联的药品 ID — 必填，指定该库存属于哪个药品
    product_name: str = Field(..., min_length=1, max_length=200)
    # 商品名称 — 必填
    manufacturer: str = Field(..., min_length=1, max_length=200)
    # 生产厂家 — 必填
    specification: str = Field(default="", max_length=100)
    # 规格 — 可选
    stock_quantity: int = Field(default=0, ge=0)
    # 初始库存数量 — 默认为 0，不允许负数
    price: float = Field(default=0.0, ge=0.0)
    # 单价 — 默认为 0.0，不允许负价
    shelf_location: str = Field(default="", max_length=50)
    # 货架位置 — 可选
    is_available: bool = True
    # 是否可售 — 默认为 True（上架）


class InventoryUpdate(BaseModel):
    """更新库存 SKU 的请求体 Schema。

    所有字段均为可选，不包含 drug_id（不允许修改外键关联）。
    库存数量不允许为负数（ge=0），单价不允许为负数（ge=0.0）。
    """
    product_name: str | None = Field(default=None, min_length=1, max_length=200)
    # 商品名称 — 可选更新
    manufacturer: str | None = Field(default=None, min_length=1, max_length=200)
    # 生产厂家 — 可选更新
    specification: str | None = Field(default=None, max_length=100)
    # 规格 — 可选更新
    stock_quantity: int | None = Field(default=None, ge=0)
    # 库存数量 — 可选更新，不允许负数
    price: float | None = Field(default=None, ge=0.0)
    # 单价 — 可选更新，不允许负数
    shelf_location: str | None = Field(default=None, max_length=50)
    # 货架位置 — 可选更新
    is_available: bool | None = None
    # 是否可售 — 可选更新


class WeightConfigItem(BaseModel):
    """权重配置的响应体 Schema — 用于 API 返回权重版本数据。"""
    id: int            # 权重配置记录唯一标识
    version: str       # 版本号（语义化版本，如 "2.1.0"）
    policy: str        # 策略名称（如 "balanced" / "conservative" / "aggressive"）
    weights: dict      # 权重字典 — 键为特征名，值为权重数值
    is_active: bool    # 是否为当前激活的版本（同一时间只有一个版本激活）
    description: str   # 该版本的描述说明
    created_at: str | None  # 创建时间（ISO 格式字符串），可能为空


class WeightConfigCreate(BaseModel):
    """创建权重配置版本的请求体 Schema。

    权重配置用于控制推荐/决策系统中各因子的影响力权重。
    版本号必须遵循语义化版本格式（如 "1.0.0"）。
    """
    version: str = Field(..., min_length=1, max_length=50, pattern=r"^\d+\.\d+\.\d+$")
    # 版本号 — 必填，必须符合语义化版本格式（X.Y.Z），由正则校验
    policy: str = Field(default="balanced", max_length=50)
    # 策略名称 — 默认为 "balanced"（均衡策略）
    weights: dict = Field(default_factory=dict)
    # 权重配置字典 — 默认为空字典，存储具体的权重键值对
    description: str = Field(default="", max_length=500)
    # 版本描述 — 可选，用于记录该版本的变更说明


# ── 药品 CRUD（增删改查）───────────────────────────────────────
#
# 以下端点提供药品数据的完整生命周期管理。
# 删除采用软删除策略：设置 deleted_at 时间戳标记为已删除，
# 而非物理删除记录，保留数据以满足审计和恢复需求。


@router.get("/drugs")
async def list_drugs(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    search: str | None = Query(default=None),
    category: str | None = Query(default=None),
    otc_type: str | None = Query(default=None),
) -> PaginatedResponse[dict]:
    """分页查询药品列表，支持多条件过滤。

    这一步的作用：为后台管理界面提供药品数据的分页展示，
    管理员可通过搜索关键词、药品分类、OTC 类型等条件筛选药品。

    参数:
        request (Request): FastAPI 请求对象，用于获取上下文信息。
        page (int): 页码，从 1 开始，默认为第 1 页，最小值为 1。
        page_size (int): 每页条数，默认 20 条，范围 1~100。
        search (str | None): 搜索关键词，按通用名称模糊匹配（ILIKE），可选。
        category (str | None): 药品分类筛选（精确匹配），可选，如"感冒退烧"。
        otc_type (str | None): OTC 类型筛选（精确匹配），可选，如"甲类"、"乙类"。

    返回:
        PaginatedResponse[dict]: 分页响应对象，包含 items（药品字典列表）、
            total（总记录数）、page（当前页码）、page_size（每页条数）。
    """
    async with get_db() as db:
        # 构建基础查询：只查询未被软删除的药品（deleted_at 为 NULL）
        base = select(Drug).where(Drug.deleted_at.is_(None))
        # 如果提供了搜索关键词，按通用名称模糊匹配（不区分大小写）
        if search:
            base = base.where(Drug.generic_name.ilike(f"%{search}%"))
        # 如果提供了分类筛选条件，精确匹配
        if category:
            base = base.where(Drug.category == category)
        # 如果提供了 OTC 类型筛选条件，精确匹配
        if otc_type:
            base = base.where(Drug.otc_type == otc_type)

        # 先统计符合条件的总记录数（用于前端分页组件）
        count_q = select(func.count()).select_from(base.subquery())
        total = (await db.execute(count_q)).scalar() or 0

        # 计算偏移量，进行分页查询
        offset = (page - 1) * page_size
        rows = (
            await db.execute(
                base.order_by(Drug.generic_name).offset(offset).limit(page_size)
            )
        ).scalars().all()

        # 将 ORM 对象转换为字典列表，封装为分页响应
        return PaginatedResponse(
            items=[_drug_to_dict(d) for d in rows],
            total=total, page=page, page_size=page_size,
        )


@router.get("/drugs/{drug_id}")
async def get_drug(drug_id: int, request: Request) -> dict:
    """获取单个药品的详细信息，并附带其所有关联的库存记录。

    这一步的作用：为药品详情页提供完整数据，不仅返回药品本身的信息，
    还一并查询该药品下的所有库存 SKU，避免前端多次请求。

    参数:
        drug_id (int): 药品 ID（路径参数），要查询的药品唯一标识。
        request (Request): FastAPI 请求对象。

    返回:
        dict: 包含药品全部字段的字典，以及一个 "inventory" 键，
            其值为该药品关联的所有库存 SKU 列表。每个库存项包含 id、
            product_name、manufacturer、specification、stock_quantity、
            price、shelf_location、is_available。

    异常:
        HTTPException(404): 当药品不存在或已被软删除时抛出。
    """
    async with get_db() as db:
        # 查询药品：必须未被软删除（deleted_at 为 NULL）
        drug = (
            await db.execute(
                select(Drug).where(Drug.id == drug_id, Drug.deleted_at.is_(None))
            )
        ).scalar_one_or_none()
        # 药品不存在则返回 404
        if drug is None:
            raise HTTPException(status_code=404, detail="Drug not found")

        # 将药品 ORM 对象转为字典，作为响应基础
        result = _drug_to_dict(drug)
        # 查询该药品关联的所有库存 SKU（排除已软删除的库存记录）
        inv_rows = (
            await db.execute(
                select(Inventory).where(
                    Inventory.drug_id == drug_id,
                    Inventory.deleted_at.is_(None),
                )
            )
        ).scalars().all()
        # 将库存 ORM 对象列表手动转换为字典列表，附加到结果中
        result["inventory"] = [
            {
                "id": i.id,
                "product_name": i.product_name,
                "manufacturer": i.manufacturer,
                "specification": i.specification,
                "stock_quantity": i.stock_quantity,
                "price": i.price,
                "shelf_location": i.shelf_location,
                "is_available": i.is_available,
            }
            for i in inv_rows
        ]
        return result


@router.post("/drugs", status_code=201)
async def create_drug(body: DrugCreate, request: Request) -> dict:
    """创建一条新的药品记录。

    这一步的作用：管理员录入新药品到数据库中。根据请求体中提供的药品信息
    创建 Drug ORM 实例并持久化到数据库，成功后返回新创建的药品数据。

    参数:
        body (DrugCreate): 请求体，包含药品的全部创建信息（通用名、分类、用法用量等）。
            已由 Pydantic 完成输入校验。
        request (Request): FastAPI 请求对象。

    返回:
        dict: 新创建的药品完整信息字典（通过 _drug_to_dict 转换）。
            HTTP 状态码为 201（Created）。
    """
    async with get_db() as db:
        # 根据请求体数据构造 Drug ORM 实例
        drug = Drug(
            generic_name=body.generic_name,
            brand_names=body.brand_names,
            category=body.category,
            active_ingredients=body.active_ingredients,
            dosage_form=body.dosage_form,
            strength=body.strength,
            otc_type=body.otc_type,
            indication_summary=body.indication_summary,
            usage_adult=body.usage_adult,
            usage_child=body.usage_child,
            usage_elderly=body.usage_elderly,
        )
        # 将新药品加入数据库会话
        db.add(drug)
        # 提交事务，将数据写入数据库
        await db.commit()
        # 刷新实例，获取数据库自动生成的字段（如 id、created_at 等）
        await db.refresh(drug)
        # 转换为字典并返回
        return _drug_to_dict(drug)


@router.put("/drugs/{drug_id}")
async def update_drug(drug_id: int, body: DrugUpdate, request: Request) -> dict:
    """更新已有药品的信息（部分更新）。

    这一步的作用：管理员修改药品的某些字段。由于使用 PATCH 语义，
    请求中只需提供要修改的字段，其他字段保持不变。已软删除的药品不可编辑。

    参数:
        drug_id (int): 要更新的药品 ID（路径参数）。
        body (DrugUpdate): 请求体，所有字段均为可选，仅更新提供的字段。
        request (Request): FastAPI 请求对象。

    返回:
        dict: 更新后的药品完整信息字典。

    异常:
        HTTPException(404): 当药品不存在或已被软删除时抛出。
    """
    async with get_db() as db:
        # 查询要更新的药品：必须未被软删除
        drug = (
            await db.execute(
                select(Drug).where(Drug.id == drug_id, Drug.deleted_at.is_(None))
            )
        ).scalar_one_or_none()
        # 药品不存在则返回 404
        if drug is None:
            raise HTTPException(status_code=404, detail="Drug not found")

        # 遍历请求体中实际提供的字段（exclude_unset=True 排除未设置的字段），
        # 逐一更新到 ORM 对象上，未提供的字段保持原值
        for field, value in body.model_dump(exclude_unset=True).items():
            setattr(drug, field, value)

        # 提交更新到数据库
        await db.commit()
        # 刷新以获取最新的数据库状态
        await db.refresh(drug)
        return _drug_to_dict(drug)


@router.delete("/drugs/{drug_id}")
async def delete_drug(drug_id: int, request: Request):
    """软删除药品（设置 deleted_at 时间戳，而非物理删除记录）。

    这一步的作用：标记药品为已删除状态。被软删除的药品不会出现在列表和详情查询中，
    但数据仍然保留在数据库中，可满足审计追溯和数据恢复的需求。
    软删除时不会级联检查关联的库存或反馈记录，以保持数据完整性。

    参数:
        drug_id (int): 要删除的药品 ID（路径参数）。
        request (Request): FastAPI 请求对象。

    返回:
        dict: 包含 success 标志、提示信息和被删除药品 ID 的响应字典。

    异常:
        HTTPException(404): 当药品不存在或已被软删除时抛出。
    """
    async with get_db() as db:
        # 查询要删除的药品：必须未被软删除
        drug = (
            await db.execute(
                select(Drug).where(Drug.id == drug_id, Drug.deleted_at.is_(None))
            )
        ).scalar_one_or_none()
        if drug is None:
            raise HTTPException(status_code=404, detail="Drug not found")

        # 设置软删除时间戳（UTC 时间），标记该记录为已删除
        drug.deleted_at = datetime.now(timezone.utc)
        # 提交事务
        await db.commit()
        return {"success": True, "message": f"Drug '{drug.generic_name}' soft-deleted", "id": drug_id}


# ── 库存 CRUD（增删改查）───────────────────────────────────────
#
# 以下端点管理药品的库存 SKU（Stock Keeping Unit，库存量单位）。
# 一个药品可以关联多个库存记录（不同厂家、不同规格），
# 支持按药品 ID、可用状态、低库存预警等条件筛选。


@router.get("/inventory")
async def list_inventory(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    drug_id: int | None = Query(default=None),
    is_available: bool | None = Query(default=None),
    stock_low: bool = Query(default=False, description="仅显示库存紧张 (<10)"),
) -> PaginatedResponse[InventoryItem]:
    """分页查询库存列表，支持多条件筛选和库存预警。

    这一步的作用：为后台库存管理页面提供分页数据。管理员可按药品、可售状态
    筛选，还可以开启"库存紧张"模式快速定位需要补货的 SKU（库存 > 0 且 < 10）。

    参数:
        request (Request): FastAPI 请求对象。
        page (int): 页码，从 1 开始，默认为第 1 页。
        page_size (int): 每页条数，默认 20 条，范围 1~100。
        drug_id (int | None): 按药品 ID 筛选，只返回该药品下的库存，可选。
        is_available (bool | None): 按可售状态筛选，True=上架，False=下架，可选。
        stock_low (bool): 是否仅显示库存紧张的 SKU（0 < 库存量 < 10），
            用于补货预警。默认为 False。

    返回:
        PaginatedResponse[InventoryItem]: 分页响应，items 为 InventoryItem 对象列表。
    """
    async with get_db() as db:
        # 构建基础查询：只查询未被软删除的库存记录
        base = select(Inventory).where(Inventory.deleted_at.is_(None))
        # 按药品 ID 筛选
        if drug_id is not None:
            base = base.where(Inventory.drug_id == drug_id)
        # 按可售状态筛选
        if is_available is not None:
            base = base.where(Inventory.is_available == is_available)
        # 库存紧张模式：筛选库存量在 0 到 10 之间的 SKU（有库存但即将耗尽）
        if stock_low:
            base = base.where(Inventory.stock_quantity < 10)
            base = base.where(Inventory.stock_quantity > 0)

        # 统计符合条件的总记录数
        count_q = select(func.count()).select_from(base.subquery())
        total = (await db.execute(count_q)).scalar() or 0

        # 分页查询
        offset = (page - 1) * page_size
        rows = (
            await db.execute(
                base.order_by(Inventory.product_name).offset(offset).limit(page_size)
            )
        ).scalars().all()

        # 将 ORM 对象转换为 InventoryItem Pydantic 模型
        items = [
            InventoryItem(
                id=r.id, drug_id=r.drug_id, product_name=r.product_name,
                manufacturer=r.manufacturer, specification=r.specification,
                stock_quantity=r.stock_quantity, price=r.price,
                shelf_location=r.shelf_location, is_available=r.is_available,
            )
            for r in rows
        ]

        return PaginatedResponse(
            items=items, total=total, page=page, page_size=page_size,
        )


@router.post("/inventory", status_code=201)
async def create_inventory(body: InventoryCreate, request: Request) -> InventoryItem:
    """创建一条新的库存 SKU 记录。

    这一步的作用：为某个药品新增库存 SKU，包括商品名称、厂家、规格、初始库存量、
    单价、货架位置等信息。创建成功后返回完整的 InventoryItem 对象。

    参数:
        body (InventoryCreate): 请求体，包含 drug_id（关联药品）、商品名称、厂家、
            规格、初始库存量、单价等信息。已由 Pydantic 完成校验。
        request (Request): FastAPI 请求对象。

    返回:
        InventoryItem: 新创建的库存 SKU 完整信息。
            HTTP 状态码为 201（Created）。
    """
    async with get_db() as db:
        # 直接将请求体解包为 Inventory ORM 实例的构造参数
        item = Inventory(**body.model_dump())
        # 加入数据库会话
        db.add(item)
        # 提交事务
        await db.commit()
        # 刷新以获取自动生成的字段（如 id）
        await db.refresh(item)
        # 构造并返回 InventoryItem 响应对象
        return InventoryItem(
            id=item.id, drug_id=item.drug_id, product_name=item.product_name,
            manufacturer=item.manufacturer, specification=item.specification,
            stock_quantity=item.stock_quantity, price=item.price,
            shelf_location=item.shelf_location, is_available=item.is_available,
        )


@router.put("/inventory/{inv_id}")
async def update_inventory(inv_id: int, body: InventoryUpdate, request: Request) -> InventoryItem:
    """更新库存 SKU 的部分字段。

    这一步的作用：管理员修改库存信息（如库存数量、单价、货架位置等）。
    不允许修改 drug_id（外键关联不可变），只能修改 SKU 自身的属性。
    已软删除的库存不可编辑。

    参数:
        inv_id (int): 库存记录 ID（路径参数），要更新的库存唯一标识。
        body (InventoryUpdate): 请求体，所有字段均为可选，仅更新提供的字段。
            drug_id 不包含在此 Schema 中，不可修改。
        request (Request): FastAPI 请求对象。

    返回:
        InventoryItem: 更新后的库存 SKU 完整信息。

    异常:
        HTTPException(404): 当库存记录不存在或已被软删除时抛出。
    """
    async with get_db() as db:
        # 查询要更新的库存记录：必须未被软删除
        item = (
            await db.execute(
                select(Inventory).where(
                    Inventory.id == inv_id,
                    Inventory.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if item is None:
            raise HTTPException(status_code=404, detail="Inventory item not found")

        # 仅更新请求体中实际提供的字段，未提供的保持原值
        for field, value in body.model_dump(exclude_unset=True).items():
            setattr(item, field, value)

        # 提交更新
        await db.commit()
        await db.refresh(item)
        # 构造并返回更新后的 InventoryItem
        return InventoryItem(
            id=item.id, drug_id=item.drug_id, product_name=item.product_name,
            manufacturer=item.manufacturer, specification=item.specification,
            stock_quantity=item.stock_quantity, price=item.price,
            shelf_location=item.shelf_location, is_available=item.is_available,
        )


@router.delete("/inventory/{inv_id}")
async def delete_inventory(inv_id: int, request: Request):
    """软删除库存 SKU（设置 deleted_at 时间戳）。

    这一步的作用：标记库存记录为已删除，而非物理删除。与药品的软删除策略一致，
    保留数据用于审计。被软删除的库存不会出现在列表查询中。

    参数:
        inv_id (int): 要删除的库存记录 ID（路径参数）。
        request (Request): FastAPI 请求对象。

    返回:
        dict: 包含 success 标志、提示信息和被删除库存 ID 的响应字典。

    异常:
        HTTPException(404): 当库存记录不存在或已被软删除时抛出。
    """
    async with get_db() as db:
        # 查询要删除的库存记录：必须未被软删除
        item = (
            await db.execute(
                select(Inventory).where(
                    Inventory.id == inv_id,
                    Inventory.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if item is None:
            raise HTTPException(status_code=404, detail="Inventory item not found")
        # 设置软删除时间戳
        item.deleted_at = datetime.now(timezone.utc)
        # 提交事务
        await db.commit()
        return {"success": True, "message": f"Inventory '{item.product_name}' soft-deleted", "id": inv_id}


# ── 权重配置管理 ───────────────────────────────────────────────
#
# 权重配置用于控制推荐/决策引擎中各因子的影响力权重。
# 系统支持多个版本并存，但同一时间只有一个版本处于激活状态。
# 通过激活/停用机制实现权重的平滑切换和 A/B 测试。


@router.get("/weights")
async def list_weights(request: Request) -> list[WeightConfigItem]:
    """获取所有权重配置版本列表，按创建时间倒序排列。

    这一步的作用：让管理员查看所有已创建的权重配置版本，
    包括激活状态、策略名称、权重详情等信息。最新创建的版本排在最前面。

    参数:
        request (Request): FastAPI 请求对象。

    返回:
        list[WeightConfigItem]: 权重配置版本列表，按 created_at 降序排列。
            每个元素包含 id、version、policy、weights 字典、is_active 状态、
            description 描述和 created_at 时间戳。
    """
    async with get_db() as db:
        # 查询所有权重配置，按创建时间降序排列（最新的在前）
        rows = (
            await db.execute(
                select(WeightConfig).order_by(WeightConfig.created_at.desc())
            )
        ).scalars().all()

        # 将 ORM 对象转换为 WeightConfigItem Pydantic 模型列表
        return [
            WeightConfigItem(
                id=r.id, version=r.version, policy=r.policy,
                weights=r.weights, is_active=r.is_active,
                description=r.description,
                # 将 datetime 对象转为 ISO 格式字符串，若为 None 则保持 None
                created_at=r.created_at.isoformat() if r.created_at else None,
            )
            for r in rows
        ]


@router.post("/weights", status_code=201)
async def create_weight(body: WeightConfigCreate, request: Request) -> WeightConfigItem:
    """创建一个新的权重配置版本。

    这一步的作用：管理员录入新版本的权重参数。新创建的版本默认不激活（is_active=False），
    需要通过专门的激活接口来启用。这样可以先创建、检查、确认后再激活。

    参数:
        body (WeightConfigCreate): 请求体，包含 version（语义化版本号）、
            policy（策略名称）、weights（权重字典）、description（版本描述）。
        request (Request): FastAPI 请求对象。

    返回:
        WeightConfigItem: 新创建的权重配置版本完整信息。
            HTTP 状态码为 201（Created）。
    """
    async with get_db() as db:
        # 构造 WeightConfig ORM 实例，新版本默认不激活
        wc = WeightConfig(
            version=body.version,
            policy=body.policy,
            weights=body.weights,
            description=body.description,
            is_active=False,  # 新版本默认不激活，需手动激活
        )
        # 加入数据库会话
        db.add(wc)
        # 提交事务
        await db.commit()
        # 刷新以获取自动生成的字段
        await db.refresh(wc)
        # 构造并返回 WeightConfigItem 响应对象
        return WeightConfigItem(
            id=wc.id, version=wc.version, policy=wc.policy,
            weights=wc.weights, is_active=wc.is_active,
            description=wc.description,
            created_at=wc.created_at.isoformat() if wc.created_at else None,
        )


@router.put("/weights/{wc_id}/activate")
async def activate_weight(wc_id: int, request: Request) -> dict:
    """激活指定的权重配置版本，同时将所有其他版本设置为非激活状态。

    这一步的作用：实现权重版本的原子切换。首先将所有当前激活的版本全部停用，
    然后将目标版本设为激活。这确保了系统中同一时间只有一个激活的权重配置。
    常用于上线新的推荐权重或进行 A/B 测试后的版本切换。

    参数:
        wc_id (int): 要激活的权重配置版本 ID（路径参数）。
        request (Request): FastAPI 请求对象。

    返回:
        dict: 包含 success 标志和 activated 版本号的响应字典。

    异常:
        HTTPException(404): 当指定的权重配置版本不存在时抛出。
    """
    async with get_db() as db:
        # 第一步：查询所有当前激活的权重配置，将其全部停用
        all_active = (
            await db.execute(
                select(WeightConfig).where(WeightConfig.is_active == True)
            )
        ).scalars().all()
        for w in all_active:
            w.is_active = False

        # 第二步：查找目标权重配置，将其设为激活状态
        target = await db.get(WeightConfig, wc_id)
        if target is None:
            raise HTTPException(status_code=404, detail="Weight config not found")
        target.is_active = True

        # 第三步：提交事务（停用所有旧版本 + 激活新版本在同一事务中完成）
        await db.commit()
        return {"success": True, "activated": target.version}
