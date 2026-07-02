"""
SQLAlchemy ORM models — OTC drug recommendation system.

定义了系统所有数据库表的结构（ORM 映射）。
每个类对应一张 PostgreSQL 表。

表关系：
  drugs  ←──1:N──  inventory        (一种药品有多个库存SKU)
  sessions ←──1:N── messages        (一个会话有多条消息)
  sessions ←──1:N── safety_logs    (一个会话有多条安全日志)
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


# ────────────────────────────────────────────────────────────
# 药品主表
# ────────────────────────────────────────────────────────────

class Drug(Base):
    """药品信息主表。

    每行代表一种 OTC 药品（按通用名区分，如"布洛芬""对乙酰氨基酚"）。
    包含药品的基本属性、适应症、用法用量等结构化信息。

    关系：一对多 → Inventory（一种药品可以有多个厂家的不同规格库存）
    """

    __tablename__ = "drugs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    """自增主键"""

    generic_name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    """药品通用名，如"布洛芬""对乙酰氨基酚""复方氨酚烷胺"。建了唯一索引，支持精确查询"""

    brand_names: Mapped[list] = mapped_column(JSON, default=list)
    """商品名列表（JSON 数组），如 ["芬必得", "美林", "布洛芬缓释胶囊"]"""

    category: Mapped[str] = mapped_column(String(50), nullable=False, default="感冒退烧")
    """药品作用类别，如"感冒退烧""解热镇痛""抗过敏"。目前 MVP 阶段主要用"感冒退烧" """

    active_ingredients: Mapped[list] = mapped_column(JSON, default=list)
    """有效成分列表（JSON 数组），如 ["布洛芬"] 或 ["对乙酰氨基酚", "盐酸伪麻黄碱"]"""

    dosage_form: Mapped[str] = mapped_column(String(50), nullable=False)
    """剂型，如"片剂""胶囊""颗粒""口服液""混悬液" """

    strength: Mapped[str] = mapped_column(String(50), nullable=False)
    """规格/含量，如"0.3g""500mg""15ml:0.6g" """

    otc_type: Mapped[str] = mapped_column(String(10), nullable=False, default="甲类")
    """OTC 类别：'甲类'（红标，需药师指导）或 '乙类'（绿标，更安全）"""

    indication_summary: Mapped[str] = mapped_column(Text, nullable=False)
    """适应症摘要，如"用于缓解轻至中度疼痛如头痛、关节痛、牙痛，也用于普通感冒引起的发热" """

    usage_adult: Mapped[str] = mapped_column(Text, nullable=False)
    """成人用法用量，如"一次1粒，一日2次（早晚各一次）" """

    usage_child: Mapped[str | None] = mapped_column(Text, nullable=True)
    """儿童用法用量，可为空（部分药品无儿童专用说明）"""

    usage_elderly: Mapped[str | None] = mapped_column(Text, nullable=True)
    """老人用法用量，可为空"""

    # ── 关系 ──
    inventory_items: Mapped[list["Inventory"]] = relationship(
        back_populates="drug", cascade="all, delete-orphan"
    )
    """关联的库存记录（一对多）。删除药品时级联删除所有关联库存"""


# ────────────────────────────────────────────────────────────
# 库存表
# ────────────────────────────────────────────────────────────

class Inventory(Base):
    """药品库存 / SKU 表。

    每行代表一个具体的可售卖商品（某个药品的某个厂家/规格/价格）。
    一种 Drug 可以有多个 Inventory 行（不同厂家、不同规格）。

    查询时通常加 is_available=True 过滤，只推荐有库存的商品。
    """

    __tablename__ = "inventory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    """自增主键"""

    drug_id: Mapped[int] = mapped_column(ForeignKey("drugs.id"), nullable=False, index=True)
    """外键 → drugs.id。建了索引，方便按药品查库存"""

    product_name: Mapped[str] = mapped_column(String(200), nullable=False)
    """商品全名，如"布洛芬缓释胶囊""美林布洛芬混悬液" """

    manufacturer: Mapped[str] = mapped_column(String(100), nullable=False)
    """厂家名称，如"中美天津史克""上海强生" """

    specification: Mapped[str] = mapped_column(String(100), nullable=False)
    """包装规格，如"0.3g×24粒""100ml:2g" """

    stock_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    """当前库存数量。0 表示缺货，>0 表示有货。库存紧张（<10）时前端应用特别标出"""

    price: Mapped[float] = mapped_column(Float, nullable=False)
    """销售单价（元）"""

    shelf_location: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    """货架位置，如"A-3-2"。方便店员或顾客找到药品"""

    is_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    """是否可售。False 表示下架/停售（不一定是缺货，即使 stock>0 也可以标记不可售）"""

    # ── 关系 ──
    drug: Mapped["Drug"] = relationship(back_populates="inventory_items")
    """回指关联的 Drug"""


# ────────────────────────────────────────────────────────────
# 会话表
# ────────────────────────────────────────────────────────────

class Session(Base):
    """用户会话表。

    每行代表一次药店咨询对话。会话是匿名的（不需要登录），用 UUID 标识。
    默认 30 分钟无活动自动过期。

    关系：一对多 → Message、一对多 → SafetyLog
    """

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    """自增主键（内部使用）"""

    session_id: Mapped[str] = mapped_column(
        String(36), unique=True, nullable=False, index=True,
        default=lambda: str(uuid.uuid4()),
    )
    """对外暴露的会话标识，UUID v4 字符串。
    前端通过此 ID 调用 POST /api/v1/chat/{session_id}"""

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active"
    )
    """会话状态：
      - 'active'  ← 正常可用
      - 'expired' ← 超过 30 分钟未活动（由 repo.get() 自动检测并标记）
      - 'closed'  ← 主动关闭"""

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    """过期时间（创建时间 + expire_minutes 分钟）。每次查询会话时，若 now > expires_at
       且 status=='active'，自动标记为 expired"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    """创建时间"""

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    """最后更新时间（每次添加消息时更新）"""

    # ── 关系 ──
    messages: Mapped[list["Message"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    """关联的消息记录（一对多）。删除会话时级联删除所有消息"""


# ────────────────────────────────────────────────────────────
# 消息表
# ────────────────────────────────────────────────────────────

class Message(Base):
    """对话消息表。

    每行记录一次用户或 AI 的发言。属于某个 Session。
    """

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    """自增主键"""

    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"), nullable=False, index=True)
    """外键 → sessions.id。建了索引，方便按会话查历史消息"""

    role: Mapped[str] = mapped_column(String(20), nullable=False)
    """发言角色：'user'（用户）或 'assistant'（AI）"""

    content: Mapped[str] = mapped_column(Text, nullable=False)
    """消息正文"""

    intent: Mapped[str | None] = mapped_column(String(50), nullable=True)
    """用户意图标签（仅用户消息），如 'describe_symptom' / 'ask_drug' / 'give_up'。
       由 Dispatcher 节点识别后写入"""

    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True)
    """扩展元数据（JSON），如 {"phase": "recommending", "node": "recommend"}。
       注意字段名带下划线后缀，避免 Python 关键字冲突"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    """消息创建时间"""

    # ── 关系 ──
    session: Mapped["Session"] = relationship(back_populates="messages")
    """回指关联的 Session"""


# ────────────────────────────────────────────────────────────
# 安全日志表
# ────────────────────────────────────────────────────────────

class SafetyLog(Base):
    """安全规则执行日志表。

    每行记录一次安全规则引擎的检查结果，用于审计和追溯。
    每个 session 可以有多条日志（每次 safety_check 节点运行都会产生）"""

    __tablename__ = "safety_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    """自增主键"""

    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"), nullable=False, index=True)
    """外键 → sessions.id。建了索引"""

    verdict: Mapped[str] = mapped_column(String(20), nullable=False)
    """检查结论：'PASS'（通过）/ 'BLOCK'（拦截）/ 'FILTER'（部分排除）"""

    triggered_rules: Mapped[list] = mapped_column(JSON, default=list)
    """触发的规则列表（JSON），如 [{"rule_id":"r6_drug_allergy","action":"FILTER","reason":"..."}] """

    input_slots: Mapped[dict] = mapped_column(JSON, default=dict)
    """触发时的症状槽位快照（方便事后追溯用户当时说了什么症状）"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    """日志创建时间"""
