"""
Admin 数据库管理 — 药品 / 库存 / 权重配置 CRUD。

GET/POST/PUT/DELETE  /api/v1/admin/database/drugs
GET/POST/PUT/DELETE  /api/v1/admin/database/inventory
GET/POST             /api/v1/admin/database/weights
PUT                  /api/v1/admin/database/weights/{id}/activate
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

router = APIRouter(prefix="/database", tags=["admin"])


# ── Helpers ──────────────────────────────────────────────────


def _drug_to_dict(d: Drug) -> dict:
    return {
        "id": d.id,
        "generic_name": d.generic_name,
        "brand_names": d.brand_names,
        "category": d.category,
        "active_ingredients": d.active_ingredients,
        "dosage_form": d.dosage_form,
        "strength": d.strength,
        "otc_type": d.otc_type,
        "indication_summary": d.indication_summary,
        "usage_adult": d.usage_adult,
        "usage_child": d.usage_child,
        "usage_elderly": d.usage_elderly,
    }


# ── Schema ──────────────────────────────────────────────────


class DrugCreate(BaseModel):
    generic_name: str = Field(..., min_length=1, max_length=200)
    brand_names: list = Field(default_factory=list)
    category: str = Field(default="感冒退烧", min_length=1, max_length=50)
    active_ingredients: list = Field(default_factory=list)
    dosage_form: str = Field(default="", max_length=50)
    strength: str = Field(default="", max_length=100)
    otc_type: str = Field(default="甲类", max_length=20)
    indication_summary: str = Field(default="", max_length=500)
    usage_adult: str = Field(default="", max_length=1000)
    usage_child: str | None = Field(default=None, max_length=1000)
    usage_elderly: str | None = Field(default=None, max_length=1000)


class DrugUpdate(BaseModel):
    generic_name: str | None = Field(default=None, min_length=1, max_length=200)
    brand_names: list | None = None
    category: str | None = Field(default=None, min_length=1, max_length=50)
    active_ingredients: list | None = None
    dosage_form: str | None = None
    strength: str | None = None
    otc_type: str | None = None
    indication_summary: str | None = None
    usage_adult: str | None = None
    usage_child: str | None = None
    usage_elderly: str | None = None


class InventoryItem(BaseModel):
    id: int
    drug_id: int
    product_name: str
    manufacturer: str
    specification: str
    stock_quantity: int
    price: float
    shelf_location: str
    is_available: bool


class InventoryCreate(BaseModel):
    drug_id: int
    product_name: str = Field(..., min_length=1, max_length=200)
    manufacturer: str = Field(..., min_length=1, max_length=200)
    specification: str = Field(default="", max_length=100)
    stock_quantity: int = Field(default=0, ge=0)
    price: float = Field(default=0.0, ge=0.0)
    shelf_location: str = Field(default="", max_length=50)
    is_available: bool = True


class InventoryUpdate(BaseModel):
    product_name: str | None = Field(default=None, min_length=1, max_length=200)
    manufacturer: str | None = Field(default=None, min_length=1, max_length=200)
    specification: str | None = Field(default=None, max_length=100)
    stock_quantity: int | None = Field(default=None, ge=0)
    price: float | None = Field(default=None, ge=0.0)
    shelf_location: str | None = Field(default=None, max_length=50)
    is_available: bool | None = None


class WeightConfigItem(BaseModel):
    id: int
    version: str
    policy: str
    weights: dict
    is_active: bool
    description: str
    created_at: str | None


class WeightConfigCreate(BaseModel):
    version: str = Field(..., min_length=1, max_length=50, pattern=r"^\d+\.\d+\.\d+$")
    policy: str = Field(default="balanced", max_length=50)
    weights: dict = Field(default_factory=dict)
    description: str = Field(default="", max_length=500)


# ── 药品 CRUD ───────────────────────────────────────────────


@router.get("/drugs")
async def list_drugs(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    search: str | None = Query(default=None),
    category: str | None = Query(default=None),
    otc_type: str | None = Query(default=None),
) -> PaginatedResponse[dict]:
    """分页查询药品列表。"""
    async with get_db() as db:
        base = select(Drug).where(Drug.deleted_at.is_(None))
        if search:
            base = base.where(Drug.generic_name.ilike(f"%{search}%"))
        if category:
            base = base.where(Drug.category == category)
        if otc_type:
            base = base.where(Drug.otc_type == otc_type)

        count_q = select(func.count()).select_from(base.subquery())
        total = (await db.execute(count_q)).scalar() or 0

        offset = (page - 1) * page_size
        rows = (
            await db.execute(
                base.order_by(Drug.generic_name).offset(offset).limit(page_size)
            )
        ).scalars().all()

        return PaginatedResponse(
            items=[_drug_to_dict(d) for d in rows],
            total=total, page=page, page_size=page_size,
        )


@router.get("/drugs/{drug_id}")
async def get_drug(drug_id: int, request: Request) -> dict:
    """获取药品详情（含关联库存）。已软删除的药品返回 404。"""
    async with get_db() as db:
        drug = (
            await db.execute(
                select(Drug).where(Drug.id == drug_id, Drug.deleted_at.is_(None))
            )
        ).scalar_one_or_none()
        if drug is None:
            raise HTTPException(status_code=404, detail="Drug not found")

        result = _drug_to_dict(drug)
        # 关联库存（排除已软删除的）
        inv_rows = (
            await db.execute(
                select(Inventory).where(
                    Inventory.drug_id == drug_id,
                    Inventory.deleted_at.is_(None),
                )
            )
        ).scalars().all()
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
    """创建新药品。"""
    async with get_db() as db:
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
        db.add(drug)
        await db.commit()
        await db.refresh(drug)
        return _drug_to_dict(drug)


@router.put("/drugs/{drug_id}")
async def update_drug(drug_id: int, body: DrugUpdate, request: Request) -> dict:
    """更新药品信息（已软删除的药品不可编辑）。"""
    async with get_db() as db:
        drug = (
            await db.execute(
                select(Drug).where(Drug.id == drug_id, Drug.deleted_at.is_(None))
            )
        ).scalar_one_or_none()
        if drug is None:
            raise HTTPException(status_code=404, detail="Drug not found")

        for field, value in body.model_dump(exclude_unset=True).items():
            setattr(drug, field, value)

        await db.commit()
        await db.refresh(drug)
        return _drug_to_dict(drug)


@router.delete("/drugs/{drug_id}")
async def delete_drug(drug_id: int, request: Request):
    """软删除药品（设置 deleted_at）。

    不检查关联库存/反馈 — 软删除保留数据完整性。
    被软删除的药品不会出现在列表和详情中。
    """
    async with get_db() as db:
        drug = (
            await db.execute(
                select(Drug).where(Drug.id == drug_id, Drug.deleted_at.is_(None))
            )
        ).scalar_one_or_none()
        if drug is None:
            raise HTTPException(status_code=404, detail="Drug not found")

        drug.deleted_at = datetime.now(timezone.utc)
        await db.commit()
        return {"success": True, "message": f"Drug '{drug.generic_name}' soft-deleted", "id": drug_id}


# ── 库存 CRUD ───────────────────────────────────────────────


@router.get("/inventory")
async def list_inventory(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    drug_id: int | None = Query(default=None),
    is_available: bool | None = Query(default=None),
    stock_low: bool = Query(default=False, description="仅显示库存紧张 (<10)"),
) -> PaginatedResponse[InventoryItem]:
    """分页查询库存列表。"""
    async with get_db() as db:
        base = select(Inventory).where(Inventory.deleted_at.is_(None))
        if drug_id is not None:
            base = base.where(Inventory.drug_id == drug_id)
        if is_available is not None:
            base = base.where(Inventory.is_available == is_available)
        if stock_low:
            base = base.where(Inventory.stock_quantity < 10)
            base = base.where(Inventory.stock_quantity > 0)

        count_q = select(func.count()).select_from(base.subquery())
        total = (await db.execute(count_q)).scalar() or 0

        offset = (page - 1) * page_size
        rows = (
            await db.execute(
                base.order_by(Inventory.product_name).offset(offset).limit(page_size)
            )
        ).scalars().all()

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
    """新增库存 SKU。"""
    async with get_db() as db:
        item = Inventory(**body.model_dump())
        db.add(item)
        await db.commit()
        await db.refresh(item)
        return InventoryItem(
            id=item.id, drug_id=item.drug_id, product_name=item.product_name,
            manufacturer=item.manufacturer, specification=item.specification,
            stock_quantity=item.stock_quantity, price=item.price,
            shelf_location=item.shelf_location, is_available=item.is_available,
        )


@router.put("/inventory/{inv_id}")
async def update_inventory(inv_id: int, body: InventoryUpdate, request: Request) -> InventoryItem:
    """更新库存 SKU（不含 drug_id，不可修改外键；已软删除的不可编辑）。"""
    async with get_db() as db:
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

        for field, value in body.model_dump(exclude_unset=True).items():
            setattr(item, field, value)

        await db.commit()
        await db.refresh(item)
        return InventoryItem(
            id=item.id, drug_id=item.drug_id, product_name=item.product_name,
            manufacturer=item.manufacturer, specification=item.specification,
            stock_quantity=item.stock_quantity, price=item.price,
            shelf_location=item.shelf_location, is_available=item.is_available,
        )


@router.delete("/inventory/{inv_id}")
async def delete_inventory(inv_id: int, request: Request):
    """软删除库存 SKU（设置 deleted_at）。"""
    async with get_db() as db:
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
        item.deleted_at = datetime.now(timezone.utc)
        await db.commit()
        return {"success": True, "message": f"Inventory '{item.product_name}' soft-deleted", "id": inv_id}


# ── 权重配置 ─────────────────────────────────────────────────


@router.get("/weights")
async def list_weights(request: Request) -> list[WeightConfigItem]:
    """获取所有权重配置版本。"""
    async with get_db() as db:
        rows = (
            await db.execute(
                select(WeightConfig).order_by(WeightConfig.created_at.desc())
            )
        ).scalars().all()

        return [
            WeightConfigItem(
                id=r.id, version=r.version, policy=r.policy,
                weights=r.weights, is_active=r.is_active,
                description=r.description,
                created_at=r.created_at.isoformat() if r.created_at else None,
            )
            for r in rows
        ]


@router.post("/weights", status_code=201)
async def create_weight(body: WeightConfigCreate, request: Request) -> WeightConfigItem:
    """创建新权重版本。"""
    async with get_db() as db:
        wc = WeightConfig(
            version=body.version,
            policy=body.policy,
            weights=body.weights,
            description=body.description,
            is_active=False,
        )
        db.add(wc)
        await db.commit()
        await db.refresh(wc)
        return WeightConfigItem(
            id=wc.id, version=wc.version, policy=wc.policy,
            weights=wc.weights, is_active=wc.is_active,
            description=wc.description,
            created_at=wc.created_at.isoformat() if wc.created_at else None,
        )


@router.put("/weights/{wc_id}/activate")
async def activate_weight(wc_id: int, request: Request) -> dict:
    """激活指定权重版本（停用其他所有版本）。"""
    async with get_db() as db:
        # 停用所有
        all_active = (
            await db.execute(
                select(WeightConfig).where(WeightConfig.is_active == True)
            )
        ).scalars().all()
        for w in all_active:
            w.is_active = False

        # 激活目标
        target = await db.get(WeightConfig, wc_id)
        if target is None:
            raise HTTPException(status_code=404, detail="Weight config not found")
        target.is_active = True

        await db.commit()
        return {"success": True, "activated": target.version}
