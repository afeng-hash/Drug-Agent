"""
Admin 系统配置 — KV 配置热更新模块。

================================ 模块概述 ================================
本模块提供系统运行时配置的查询与热更新接口，属于 Admin 后台管理功能。
所有配置以 Key-Value（键值对）形式存储在数据库中，支持通过 API 动态修改，
修改后即时生效（热更新），无需重启服务。

============================= 核心设计理念 ==============================
1. 配置分为两层：
   - 数据库层（SystemConfig 表）：持久化存储，管理员通过 API 修改后写入。
   - 内存层（Settings 对象）：运行时缓存，应用启动时从数据库加载，
     通过 API 修改后同步更新内存中的值。
2. 热更新机制：
   修改配置时，先写入数据库（持久化），再通过 Python 的 setattr 更新
   Settings 对象的对应属性，使新值立即在后续请求中生效。
3. 类型安全：
   _RUNTIME_KEYS 字典定义了每个配置键的描述和类型转换器（converter），
   确保从字符串（API 传输格式）到目标类型（如 int、bool）的安全转换。

=============================== API 端点 ===============================
GET  /api/v1/admin/config  — 获取所有可热更新的配置项列表
PUT  /api/v1/admin/config  — 批量更新配置项，支持热更新到内存

=========================== 新增配置键的步骤 ===========================
1. 在 _RUNTIME_KEYS 字典中添加一行：{key: (中文描述, 类型转换器)}
2. 在 Settings 类中添加对应的字段声明
完成以上两步后，新配置键即可通过本模块的 API 进行读写。
"""

from datetime import datetime, timezone  # 用于记录配置更新时间（UTC 时区）

from fastapi import APIRouter, HTTPException, Request  # FastAPI 路由、HTTP 异常、请求对象
from pydantic import BaseModel, Field  # 数据校验与序列化
from sqlalchemy import select  # SQLAlchemy 异步查询

from app.db.database import get_db  # 数据库会话获取器
from app.db.models import SystemConfig  # 系统配置 ORM 模型

# 创建 Admin 配置路由，路径前缀为 /config，在 OpenAPI 文档中归类到 "admin" 标签
router = APIRouter(prefix="/config", tags=["admin"])


class ConfigItem(BaseModel):
    """单个系统配置项的响应模型。

    用于 GET/PUT 接口的响应体，将数据库中的配置记录序列化为 JSON 返回给前端。
    """
    key: str            # 配置键名，例如 "max_consult_rounds"
    value: str          # 配置值（字符串形式存储/传输，使用时由 converter 转换）
    description: str    # 配置项的中文描述，便于管理员理解用途
    updated_by: str     # 最后更新者的标识，默认为 "admin" 或 "system"
    updated_at: str | None  # 最后更新时间（ISO 格式字符串），从未更新过则为 None


class ConfigUpdateBody(BaseModel):
    """批量配置更新的请求体模型。

    前端通过 PUT 请求发送一个配置项列表，后端逐条处理并写入数据库。
    限制每次最少 1 条、最多 20 条，防止一次更新过多配置导致超时或误操作。
    """
    configs: list[dict] = Field(
        ...,  # ... 表示该字段为必填项
        min_length=1,      # 每次至少更新 1 条配置
        max_length=20,     # 每次最多更新 20 条配置，防止批量过大
        description="[{key: 'max_consult_rounds', value: '8', description: '...'}]",
    )


# ===========================================================================
# 运行时配置键注册表（Runtime Keys Registry）
# ===========================================================================
# 作用：定义哪些配置键可以在运行时通过 API 修改并即时生效（热更新）。
# 只有在此注册表中声明的 key，才会：
#   1. 出现在 GET /config 的返回结果中
#   2. 在 PUT /config 更新时被同步到 Settings 内存对象
#
# 格式: {key: (中文描述, 类型转换器)}
#   - key: 配置键名（字符串），需与 Settings 类中的属性名一致
#   - 中文描述: 用于在管理后台展示，帮助管理员理解配置含义
#   - 类型转换器: 可调用对象，将字符串值（API 传输格式）转换为目标 Python 类型
#     例如 int 将 "8" 转为 8，lambda 将 "true" 转为 True (bool)
#
# 新增 key 时只需在此加一行 + Settings 类中加对应字段
_RUNTIME_KEYS = {
    "max_consult_rounds": (
        "最大问诊轮数",  # 控制 Consult Agent 最多进行几轮问诊对话
        int,              # 转换器：将字符串转为整数
    ),
    "web_search_enabled": (
        "联网搜索开关",  # 控制 Agent 是否允许调用外部搜索引擎
        lambda v: v.lower() in ("true", "1", "yes"),  # 转换器：转为布尔值
    ),
    "session_expire_minutes": (
        "会话过期时间（分钟）",  # 超过此时间未活动的会话将被自动清理
        int,                     # 转换器：将字符串转为整数
    ),
}


@router.get("")
async def list_config(request: Request) -> list[ConfigItem]:
    """获取所有系统配置项列表（GET /api/v1/admin/config）。

    这一步的作用：
        从数据库和默认配置中汇总所有可热更新的系统配置项，以列表形式返回给前端管理页面。
        对于已在数据库中配置的项，返回数据库中的值；对于未在数据库中配置的项，返回 Settings 中的默认值。

    参数：
        request (Request): FastAPI 请求对象，通过 request.app.state.settings 获取全局 Settings 实例

    返回值：
        list[ConfigItem]: 所有配置项的列表，每项包含 key、value、description、updated_by、updated_at
    """
    # 从应用全局状态中获取 Settings 实例（内存中的运行时配置缓存）
    settings = request.app.state.settings

    # ----- 步骤1：从 Settings 构建默认值映射 -----
    # 将 Settings 对象的属性值统一转为字符串，作为兜底默认值
    defaults = {
        "max_consult_rounds": str(settings.max_consult_rounds),
        "web_search_enabled": str(settings.web_search_enabled),
        "session_expire_minutes": str(settings.session_expire_minutes),
    }

    # ----- 步骤2：从数据库读取已持久化的配置 -----
    async with get_db() as db:
        # 查询 SystemConfig 表中所有记录，按 key 排序
        db_configs = (
            await db.execute(select(SystemConfig).order_by(SystemConfig.key))
        ).scalars().all()
        # 构建 {key: ORM对象} 的映射，便于后续快速查找
        db_map = {c.key: c for c in db_configs}

    # ----- 步骤3：逐个构建 ConfigItem 响应对象 -----
    items = []
    for key, default_val in defaults.items():
        # 从 _RUNTIME_KEYS 获取该配置键的中文描述（用于管理后台展示）
        desc, _ = _RUNTIME_KEYS.get(key, (key, str))

        if key in db_map:
            # 情况A：数据库中存在该配置，使用数据库的值
            c = db_map[key]
            items.append(
                ConfigItem(
                    key=key,
                    value=c.value,                               # 数据库中的值
                    description=c.description or desc,           # 优先使用数据库中的描述
                    updated_by=c.updated_by,                     # 最后更新者
                    updated_at=c.updated_at.isoformat() if c.updated_at else None,  # 转为 ISO 格式字符串
                )
            )
        else:
            # 情况B：数据库中不存在该配置，使用 Settings 的默认值
            items.append(
                ConfigItem(
                    key=key,
                    value=default_val,      # Settings 中的默认值
                    description=desc,        # 中文描述
                    updated_by="system",     # 标记为系统默认生成
                    updated_at=None,         # 从未被用户更新过
                )
            )

    return items


@router.put("")
async def update_config(body: ConfigUpdateBody, request: Request) -> list[ConfigItem]:
    """批量更新系统配置（PUT /api/v1/admin/config）。

    这一步的作用：
        接收前端提交的配置更新列表，逐条写入数据库以确保持久化，同时通过
        setattr 将新值同步到内存中的 Settings 对象，实现配置的即时生效（热更新）。
        如果某个配置键有对应的类型转换器，则将字符串值转为目标类型后再写入。

    参数：
        body (ConfigUpdateBody): 请求体，包含待更新的配置项列表（1~20 条）
        request (Request): FastAPI 请求对象，用于获取 Settings 实例

    返回值：
        list[ConfigItem]: 更新后的全部配置项列表（与 GET 接口返回格式一致）

    异常：
        HTTPException 400: 当某个配置值的类型转换失败时抛出（例如 "abc" 无法转为 int）
    """
    # 获取全局 Settings 实例，用于热更新内存中的运行时值
    settings = request.app.state.settings

    # ----- 开启数据库事务，逐条处理配置更新 -----
    async with get_db() as db:
        for item in body.configs:
            # 提取当前配置项的三要素
            key = item["key"]                # 配置键名
            value = item["value"]            # 配置值（字符串形式）
            description = item.get("description", "")  # 中文描述，允许为空

            # ----- Upsert（存在则更新，不存在则插入）策略 -----
            # 先查询数据库中是否已存在该 key 的配置记录
            existing = (
                await db.execute(
                    select(SystemConfig).where(SystemConfig.key == key)
                )
            ).scalar_one_or_none()  # 返回单条记录或 None

            if existing:
                # 情况A：已存在 — 更新现有记录的字段
                existing.value = value
                existing.description = description
                existing.updated_by = item.get("updated_by", "admin")  # 记录操作者
                existing.updated_at = datetime.now(timezone.utc)       # 记录更新时间（UTC）
            else:
                # 情况B：不存在 — 创建新的配置记录并添加到数据库会话
                db.add(
                    SystemConfig(
                        key=key,
                        value=value,
                        description=description,
                        updated_by=item.get("updated_by", "admin"),  # 默认为 admin
                    )
                )

            # ----- 热更新：将新值同步到内存中的 Settings 对象 -----
            # 只有 _RUNTIME_KEYS 中注册的配置键才参与热更新
            if key in _RUNTIME_KEYS:
                _, converter = _RUNTIME_KEYS[key]  # 获取类型转换器（如 int、lambda）
                try:
                    # 通过 setattr 动态修改 Settings 对象的属性值
                    # 例如 setattr(settings, "max_consult_rounds", int("8")) => settings.max_consult_rounds = 8
                    setattr(settings, key, converter(value))
                except (ValueError, TypeError):
                    # 类型转换失败时（如 "abc" 无法转为 int），返回 400 错误
                    # 此时数据库尚未 commit，该次更新不会生效
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid value '{value}' for config key '{key}'",
                    )

        # ----- 提交事务：将所有修改持久化到数据库 -----
        await db.commit()

    # ----- 返回更新后的完整配置列表（复用 GET 接口的逻辑） -----
    return await list_config(request)
