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
# Mixin: Soft Delete（软删除模式）
# ────────────────────────────────────────────────────────────

class SoftDeleteMixin:
    """软删除 Mixin — 为表添加 deleted_at 时间戳。

    用法::

        class MyModel(Base, SoftDeleteMixin):
            __tablename__ = "my_table"
            ...

    查询时过滤已删除行::

        select(MyModel).where(MyModel.deleted_at.is_(None))

    删除操作改为软删除::

        row.deleted_at = datetime.now(timezone.utc)
        await db.commit()

    要启用全局自动过滤，可设置 __table_args__ 中的 PostgreSQL RLS，
    或在 Repository 基类中统一添加 WHERE deleted_at IS NULL 条件。

    迁移状态 (2026-07-07):
      ✅ PromptTemplate    — 已应用 (prompts.py: soft-delete + list/get filter)
      ✅ Drug              — 已应用 (database.py: soft-delete + list filter)
      ✅ Inventory         — 已应用 (database.py: soft-delete + list filter)
      ✅ HighRiskKeyword   — 已应用 (risk.py: soft-delete + list filter)
      ⬜ Skill             — 待迁移 (无 DELETE 端点，低优先级)
      ⬜ Tool              — 待迁移 (无 DELETE 端点，低优先级)
      ⬜ WeightConfig      — 待迁移 (无 DELETE 端点，低优先级)
      ⬜ Feedback          — 待迁移 (无 DELETE 端点，低优先级)
      ⬜ SkillVersion      — 待迁移 (级联 Skill，低优先级)

    迁移步骤（新增表时参考）:
      1. 继承 SoftDeleteMixin
      2. 删除端点: db.delete(obj) → obj.deleted_at = now()
      3. 查询端点: select(Model) → select(Model).where(Model.deleted_at.is_(None))
      4. 获取端点: db.get(Model, id) → 添加 deleted_at IS NULL 过滤
      5. 返回码: 204 No Content → 200 + {"success": True, ...}
    """

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None, index=True,
    )
    """软删除时间戳。NULL = 未删除，非 NULL = 已删除。"""


# ────────────────────────────────────────────────────────────
# 药品主表
# ────────────────────────────────────────────────────────────

class Drug(Base, SoftDeleteMixin):
    """药品信息主表。

    每行代表一种 OTC 药品（按通用名区分，如"布洛芬""对乙酰氨基酚"）。
    包含药品的基本属性、适应症、用法用量等结构化信息。

    关系：一对多 → Inventory（一种药品可以有多个厂家的不同规格库存）
    删除策略：软删除（deleted_at）— 保留关联的库存和反馈引用的完整性。
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

class Inventory(Base, SoftDeleteMixin):
    """药品库存 / SKU 表。

    每行代表一个具体的可售卖商品（某个药品的某个厂家/规格/价格）。
    一种 Drug 可以有多个 Inventory 行（不同厂家、不同规格）。

    查询时通常加 is_available=True 过滤，只推荐有库存的商品。
    删除策略：软删除（deleted_at）。
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

    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True,
    )
    """关联用户。NULL = 匿名用户（向后兼容）"""

    state_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    """跨 turn 结构化状态快照。

    每个 turn 结束时由 end_node 写入，下个 turn 开始时由 chat.py 读取并恢复到 state。
    存储内容：consult_slots, phase, previous_phase, consult_rounds,
             consult_summary, safety_result, recommendations, dispatcher_result

    为 None 表示新会话的首个 turn（还没有任何结构化状态需要恢复）。
    """

    # ── 关系 ──
    user: Mapped["User | None"] = relationship(back_populates="sessions")
    """回指关联的 User"""

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


# ────────────────────────────────────────────────────────────
# 权重配置表
# ────────────────────────────────────────────────────────────

class WeightConfig(Base):
    """药品评分权重配置表。

    每行代表一个权重版本。同时只有一个版本 is_active=True。
    支持 A/B 测试：通过 ab_group + ab_ratio 按 session 分桶路由。
    """

    __tablename__ = "weights_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    """自增主键"""

    version: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    """语义版本号，如 'v3.2.1'，唯一索引"""

    policy: Mapped[str] = mapped_column(String(50), nullable=False, default="balanced")
    """策略名：'balanced' | 'safety_first'"""

    scoring_version: Mapped[str | None] = mapped_column(String(10), nullable=True)
    """评分公式版本：
      - None / 'v1' → 几何加权平均（score = Π f_i^w_i, Σw_i=1.0）
      - 'v2'       → 层级乘法模型（score = sm × focus^α × age^β × otc^γ）
    """

    weights: Mapped[dict] = mapped_column(JSON, nullable=False)
    """权重/指数 JSON：
      v1: 几何权重 {"symptom_match": 0.50, "symptom_focus_ratio": 0.15, ...}
      v2: 惩罚指数 {"focus": 0.5, "age": 0.3, "otc": 0.05}
    """

    feature_defaults: Mapped[dict] = mapped_column(JSON, default=dict)
    """特征默认值 JSON（仅 v1 使用）：{"symptom_match": 0.0, "safety": 1.0, ...}"""

    safety_block_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.2)
    """安全排除阈值：safety < 此值 → 药品不推荐"""

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    """是否为当前激活版本"""

    ab_group: Mapped[str | None] = mapped_column(String(10), nullable=True)
    """A/B 分组标识：'A' / 'B' / None（全量）"""

    ab_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    """A/B 流量比例：0.0~1.0"""

    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    """变更说明"""

    changed_by: Mapped[str] = mapped_column(String(100), nullable=False, default="system")
    """操作人"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    """创建时间"""


# ═══════════════════════════════════════════════════════════════
# AI Agent 运营平台 — 新增模型
# ═══════════════════════════════════════════════════════════════


# ────────────────────────────────────────────────────────────
# 用户表
# ────────────────────────────────────────────────────────────

class User(Base):
    """用户表 — 关联多个会话，支持会话历史查询。

    匿名用户不创建 User 记录（向后兼容）。
    当用户提供外部标识（手机号/微信 ID 等）时创建。
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    """自增主键"""

    external_id: Mapped[str] = mapped_column(
        String(100), unique=True, nullable=False, index=True,
    )
    """外部标识（手机号/微信openid/设备指纹）"""

    nickname: Mapped[str | None] = mapped_column(String(100), nullable=True)
    """用户昵称/称呼"""

    health_profile: Mapped[dict] = mapped_column(JSON, default=dict)
    """累积的健康画像：
      {"allergies": [...], "chronic_conditions": [...],
       "age": 28, "special_population": "孕妇"}
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    """注册时间"""

    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    """最后活跃时间"""

    # ── 关系 ──
    sessions: Mapped[list["Session"]] = relationship(back_populates="user")
    """关联的会话列表"""


# ────────────────────────────────────────────────────────────
# LLM 调用日志表
# ────────────────────────────────────────────────────────────

class LLMCallLog(Base):
    """LLM 每次调用的完整记录。

    通过 fire-and-forget 异步写入，不阻塞主流程。
    按时间分区或定期归档以控制数据量。
    """

    __tablename__ = "llm_call_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    """自增主键"""

    session_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True,
    )
    """关联会话 UUID（非对话触发的 LLM 调用可为空）"""

    node: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    """调用节点：
      "dispatcher"|"consult"|"react"|"recommend"
      |"classifier"|"generator"
    """

    model: Mapped[str] = mapped_column(String(50), nullable=False)
    """模型名：qwen-plus / qwen-turbo / qwen-max / ..."""

    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    """输入 token 数"""

    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    """输出 token 数"""

    latency_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    """端到端延迟（毫秒）"""

    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    """是否调用成功"""

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    """错误信息（成功时为 None）"""

    turn_id: Mapped[str | None] = mapped_column(
        String(100), nullable=True, index=True,
    )
    """Turn 标识："{session_id}:{turn_seq}"，用于 turn 级别的 LLM 调用过滤"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    """调用时间"""


# ────────────────────────────────────────────────────────────
# 模型配置表
# ────────────────────────────────────────────────────────────

class ModelConfig(Base):
    """每个 LLM 调用角色的模型配置。

    替代当前硬编码在 Settings 和 builder.py 中的模型参数。
    运行时可通过 API 热更新，无需重启。
    """

    __tablename__ = "model_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    """自增主键"""

    role: Mapped[str] = mapped_column(
        String(50), unique=True, nullable=False, index=True,
    )
    """角色名（唯一）：
      "dispatcher"|"consult"|"react"|"recommend"
      |"classifier"|"generator"
    """

    model_name: Mapped[str] = mapped_column(String(50), nullable=False)
    """模型名：qwen-plus / qwen-turbo / qwen-max"""

    temperature: Mapped[float] = mapped_column(Float, nullable=False, default=0.3)
    """采样温度 0.0-2.0"""

    max_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=1024)
    """最大输出 token 数"""

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    """是否为当前激活配置（每个 role 同时只有一个 active）"""

    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    """变更说明"""

    updated_by: Mapped[str] = mapped_column(String(100), nullable=False, default="system")
    """操作人"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    """创建时间"""

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    """最后更新时间"""


# ────────────────────────────────────────────────────────────
# Prompt 模板表
# ────────────────────────────────────────────────────────────

class PromptTemplate(Base, SoftDeleteMixin):
    """Prompt 版本管理。

    支持同一 role 多版本，同时只一个 is_active。
    Phase 1 仅做管理界面 CRUD，运行时不从 DB 读（仍用代码常量）。
    """

    __tablename__ = "prompt_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    """自增主键"""

    role: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    """Prompt 角色：
      "recommend"|"react_system"|"classifier"|"generator"
      |"side_effects"|"contraindications"|"dosage"|"efficacy"
      |"special_population"|"drug_interaction"|"drug_comparison"
      |"recommendation_explanation"
    """

    version: Mapped[str] = mapped_column(String(20), nullable=False)
    """语义版本号，如 "v1.0.0" """

    content: Mapped[str] = mapped_column(Text, nullable=False)
    """Prompt 全文"""

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    """是否为当前激活版本"""

    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    """变更说明"""

    updated_by: Mapped[str] = mapped_column(String(100), nullable=False, default="system")
    """操作人"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    """创建时间"""


# ────────────────────────────────────────────────────────────
# 用户反馈表
# ────────────────────────────────────────────────────────────

class Feedback(Base):
    """用户对推荐结果的反馈。"""

    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    """自增主键"""

    session_id: Mapped[str] = mapped_column(
        String(36), nullable=False, index=True,
    )
    """关联会话 UUID"""

    drug_id: Mapped[int | None] = mapped_column(
        ForeignKey("drugs.id"), nullable=True,
    )
    """关联药品（可为空，如整体满意度评价）"""

    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    """评分：1-5（或 thumbs_up=5 / thumbs_down=1）"""

    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    """用户文字反馈"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    """反馈时间"""


# ────────────────────────────────────────────────────────────
# 操作审计日志表
# ────────────────────────────────────────────────────────────

class AdminAuditLog(Base):
    """管理后台操作审计。

    所有 admin 模块的写操作通过依赖注入写入此表。
    """

    __tablename__ = "admin_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    """自增主键"""

    admin_user: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    """操作人"""

    action: Mapped[str] = mapped_column(String(20), nullable=False)
    """操作类型：
      "create"|"update"|"delete"|"activate"|"deactivate"
    """

    resource_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    """资源类型：
      "drug"|"inventory"|"weight_config"|"model_config"
      |"prompt"|"skill"|"tool"|"risk_keyword"|"system_config"|...
    """

    resource_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    """被操作资源的 ID"""

    changes: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    """变更内容（diff），JSON 格式"""

    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    """客户端 IP 地址"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    """操作时间"""


# ────────────────────────────────────────────────────────────
# 链路追踪日志表
# ────────────────────────────────────────────────────────────

class TraceLog(Base):
    """每次 Graph 节点执行的追踪日志。

    一个 turn 产生多条 trace（每个节点一条）。
    通过 turn_id 聚合成完整链路。
    """

    __tablename__ = "trace_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    """自增主键"""

    session_id: Mapped[str] = mapped_column(
        String(36), nullable=False, index=True,
    )
    """关联会话 UUID"""

    turn_id: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True,
    )
    """Turn 标识："{session_id}:{turn_seq}"，同一次用户消息的所有节点共享"""

    node: Mapped[str] = mapped_column(String(50), nullable=False)
    """节点名：
      "intake"|"dispatcher"|"consult"|"safety_block"
      |"recommend"|"inventory"|"react"|"end"
    """

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="started")
    """执行状态："started"|"completed"|"error" """

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    """开始时间"""

    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    """完成时间"""

    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    """节点执行耗时（毫秒）"""

    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True)
    """节点级元数据：
      dispatcher: {"route": "consult", "intent": "describe_symptom", "actions": [...]}
      consult:    {"next_action": "ask"|"done", "rounds": 2}
      safety_block: {"verdict": "PASS"|"BLOCK", "triggered_rules": [...]}
      recommend:  {"count": 3, "config_version": "v3.2.1", "scoring_ms": 45.2}
      react:      {"task_type": "side_effects", "skills_used": true, "iterations": 1}
      inventory:  {"items_found": 5}
      end:        {"status": "ok"}
    """

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    """错误信息（status=error 时）"""

    error_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    """错误类型："llm_error"|"db_error"|"kg_error"|"rag_error"|"timeout" """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    """记录创建时间"""


# ────────────────────────────────────────────────────────────
# 技能定义表
# ────────────────────────────────────────────────────────────

class Skill(Base):
    """技能（Skill）定义。

    对应 task_definitions.py 中的一个 SOP。
    运营人员可通过管理界面管理技能的版本和状态。
    """

    __tablename__ = "skills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    """自增主键"""

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    """技能展示名，如 "副作用查询" """

    task_type: Mapped[str] = mapped_column(
        String(50), unique=True, nullable=False, index=True,
    )
    """对应的 TaskType 枚举值：
      "side_effects"|"contraindications"|"dosage"|"efficacy"
      |"special_population"|"drug_interaction"|"drug_comparison"
      |"recommendation_explanation"
    """

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    """状态："active"|"inactive"|"draft" """

    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    """技能描述"""

    current_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    """当前激活的版本号"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    """创建时间"""

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    """最后更新时间"""


# ────────────────────────────────────────────────────────────
# 技能版本表
# ────────────────────────────────────────────────────────────

class SkillVersion(Base):
    """技能版本 — 每个版本包含完整的 SOP 定义。

    支持版本回滚和 A/B 测试。
    """

    __tablename__ = "skill_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    """自增主键"""

    skill_id: Mapped[int] = mapped_column(
        ForeignKey("skills.id"), nullable=False, index=True,
    )
    """外键 → skills.id"""

    version: Mapped[str] = mapped_column(String(20), nullable=False)
    """语义版本号，如 "v1.0.0" """

    sop_steps: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    """SOP 步骤列表（JSON）：
      [{order, tool_name, args_template, parallel_group, is_critical, timeout_ms}, ...]
    """

    response_structure: Mapped[str] = mapped_column(Text, nullable=False, default="")
    """回复结构建议（自然语言）"""

    mandatory_reminders: Mapped[list] = mapped_column(JSON, default=list)
    """强制性安全提醒列表"""

    fallback_response: Mapped[str] = mapped_column(Text, nullable=False, default="")
    """兜底回复模板，可用 {drug_name} 等占位符"""

    changelog: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    """版本变更说明"""

    created_by: Mapped[str] = mapped_column(String(100), nullable=False, default="system")
    """创建人"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    """创建时间"""


# ────────────────────────────────────────────────────────────
# 工具注册表
# ────────────────────────────────────────────────────────────

class Tool(Base):
    """工具注册 — 管理 Agent 可用的工具。

    Phase 1 只做只读展示 + 启停，不修改 parameters_schema。
    """

    __tablename__ = "tools"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    """自增主键"""

    name: Mapped[str] = mapped_column(
        String(50), unique=True, nullable=False, index=True,
    )
    """工具名（唯一）：
      "search_manual"|"get_drug_detail"|"search_drug"
      |"search_web"|"get_recommendation"|"get_user_profile"
    """

    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    """展示名，如 "说明书检索" """

    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    """工具用途说明"""

    parameters_schema: Mapped[dict] = mapped_column(JSON, default=dict)
    """参数 Schema（OpenAI function-calling 格式）"""

    capabilities: Mapped[list] = mapped_column(JSON, default=list)
    """能力标签：["drug_manual", "rag"] / ["drug_profile"] / ..."""

    fallback_tools: Mapped[list] = mapped_column(JSON, default=list)
    """容错替代工具列表：["search_web"] """

    timeout_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=15000)
    """默认超时（毫秒）"""

    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    """重试次数"""

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    """状态："active"|"inactive"|"deprecated" """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    """创建时间"""

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    """最后更新时间"""


# ────────────────────────────────────────────────────────────
# 高风险关键字表
# ────────────────────────────────────────────────────────────

class HighRiskKeyword(Base, SoftDeleteMixin):
    """高风险关键字 — 用于监控对话中的危险信号。

    当用户消息匹配关键字时触发告警。
    删除策略：软删除（deleted_at）— 保留已触发的告警关联。
    """

    __tablename__ = "high_risk_keywords"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    """自增主键"""

    keyword: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    """关键字/短语"""

    category: Mapped[str] = mapped_column(String(50), nullable=False)
    """类别：
      "suicide"|"severe_allergy"|"emergency"|"drug_abuse"|"other"
    """

    severity: Mapped[str] = mapped_column(String(20), nullable=False, default="medium")
    """严重程度："low"|"medium"|"high"|"critical" """

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    """是否启用"""

    negative_patterns: Mapped[str | None] = mapped_column(
        String(500), nullable=True,
    )
    """白名单正则（逗号分隔）。

    当关键字命中后，若内容同时匹配 negative_patterns 中任一模式，
    则抑制告警（视为合法医疗内容，减少误匹配）。

    例如：关键字 "毒品" 的 negative_patterns = "药品|解毒|消毒"
    文本"消毒品"命中关键字但同时命中白名单"消毒"→ 不告警。
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    """创建时间"""


# ────────────────────────────────────────────────────────────
# 高风险告警表
# ────────────────────────────────────────────────────────────

class HighRiskAlert(Base):
    """高风险告警 — 关键字匹配触发的告警记录。"""

    __tablename__ = "high_risk_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    """自增主键"""

    session_id: Mapped[str] = mapped_column(
        String(36), nullable=False, index=True,
    )
    """关联会话 UUID"""

    keyword_id: Mapped[int | None] = mapped_column(
        ForeignKey("high_risk_keywords.id"), nullable=True,
    )
    """关联关键字（关键字可能被删除故 nullable）"""

    matched_content: Mapped[str] = mapped_column(Text, nullable=False)
    """触发告警的消息内容（截取匹配部分的前后文）"""

    is_reviewed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    """是否已处理"""

    reviewed_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    """处理人"""

    review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    """处理备注"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    """触发时间"""


# ────────────────────────────────────────────────────────────
# 系统配置表
# ────────────────────────────────────────────────────────────

class SystemConfig(Base):
    """系统配置 KV 存储。

    支持运行时热更新的配置项。基础设施类配置（数据库 URL 等）仍走 .env。
    """

    __tablename__ = "system_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    """自增主键"""

    key: Mapped[str] = mapped_column(
        String(100), unique=True, nullable=False, index=True,
    )
    """配置键（唯一）：
      "max_consult_rounds"|"web_search_enabled"|"session_expire_minutes"|...
    """

    value: Mapped[str] = mapped_column(Text, nullable=False)
    """配置值（统一存为字符串，读取时按需转换类型）"""

    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    """配置说明"""

    updated_by: Mapped[str] = mapped_column(String(100), nullable=False, default="system")
    """最后修改人"""

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    """最后更新时间"""


# ────────────────────────────────────────────────────────────
# 管理员用户表
# ────────────────────────────────────────────────────────────

class AdminUser(Base):
    """管理后台用户 — Phase 1 预留，暂不启用认证。"""

    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    """自增主键"""

    username: Mapped[str] = mapped_column(
        String(100), unique=True, nullable=False, index=True,
    )
    """用户名"""

    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    """密码哈希（bcrypt/argon2）"""

    role: Mapped[str] = mapped_column(String(20), nullable=False, default="viewer")
    """角色："admin"|"operator"|"viewer" """

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    """是否启用"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    """创建时间"""
