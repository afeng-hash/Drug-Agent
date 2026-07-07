"""
Admin 系统配置 — KV 配置热更新。

GET  /api/v1/admin/config  — 所有可热更新配置
PUT  /api/v1/admin/config  — 更新配置
"""

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.db.database import get_db
from app.db.models import SystemConfig

router = APIRouter(prefix="/config", tags=["admin"])


class ConfigItem(BaseModel):
    key: str
    value: str
    description: str
    updated_by: str
    updated_at: str | None


class ConfigUpdateBody(BaseModel):
    configs: list[dict] = Field(
        ..., min_length=1, max_length=20,
        description="[{key: 'max_consult_rounds', value: '8', description: '...'}]",
    )


# 已知的运行时配置键（其值影响内存中的行为）
# 格式: {key: (description, type_converter)}
# 新增 key 时只需在此加一行 + Settings 类中加对应字段
_RUNTIME_KEYS = {
    "max_consult_rounds": ("最大问诊轮数", int),
    "web_search_enabled": ("联网搜索开关", lambda v: v.lower() in ("true", "1", "yes")),
    "session_expire_minutes": ("会话过期时间（分钟）", int),
}


@router.get("")
async def list_config(request: Request) -> list[ConfigItem]:
    """获取所有系统配置项。

    先从 DB 读取，未在 DB 配置的则从 Settings 取默认值。
    """
    settings = request.app.state.settings

    # 从 Settings 构建默认值
    defaults = {
        "max_consult_rounds": str(settings.max_consult_rounds),
        "web_search_enabled": str(settings.web_search_enabled),
        "session_expire_minutes": str(settings.session_expire_minutes),
    }

    async with get_db() as db:
        # DB 中的配置
        db_configs = (
            await db.execute(select(SystemConfig).order_by(SystemConfig.key))
        ).scalars().all()
        db_map = {c.key: c for c in db_configs}

    items = []
    for key, default_val in defaults.items():
        desc, _ = _RUNTIME_KEYS.get(key, (key, str))
        if key in db_map:
            c = db_map[key]
            items.append(
                ConfigItem(
                    key=key,
                    value=c.value,
                    description=c.description or desc,
                    updated_by=c.updated_by,
                    updated_at=c.updated_at.isoformat() if c.updated_at else None,
                )
            )
        else:
            items.append(
                ConfigItem(
                    key=key,
                    value=default_val,
                    description=desc,
                    updated_by="system",
                    updated_at=None,
                )
            )

    return items


@router.put("")
async def update_config(body: ConfigUpdateBody, request: Request) -> list[ConfigItem]:
    """批量更新系统配置。

    写入 DB 的同时更新运行时的 Settings 内存缓存。
    """
    settings = request.app.state.settings

    async with get_db() as db:
        for item in body.configs:
            key = item["key"]
            value = item["value"]
            description = item.get("description", "")

            # Upsert: 查找已有或新建
            existing = (
                await db.execute(
                    select(SystemConfig).where(SystemConfig.key == key)
                )
            ).scalar_one_or_none()

            if existing:
                existing.value = value
                existing.description = description
                existing.updated_by = item.get("updated_by", "admin")
                existing.updated_at = datetime.now(timezone.utc)
            else:
                db.add(
                    SystemConfig(
                        key=key,
                        value=value,
                        description=description,
                        updated_by=item.get("updated_by", "admin"),
                    )
                )

            # 热更新 Settings 运行时值（通过 _RUNTIME_KEYS 查找转换器）
            if key in _RUNTIME_KEYS:
                _, converter = _RUNTIME_KEYS[key]
                try:
                    setattr(settings, key, converter(value))
                except (ValueError, TypeError):
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid value '{value}' for config key '{key}'",
                    )

        await db.commit()

    # 返回更新后的配置
    return await list_config(request)
