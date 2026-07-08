"""
Admin 用户访问分析 — 仪表盘数据模块。

本模块为后台管理面板提供数据分析接口，帮助运营人员了解系统使用情况和用户行为。
所有接口均支持通过 `days` 参数指定统计的时间范围（默认 30 天），数据来源于数据库中的
Session（会话）、Message（消息）和 SafetyLog（安全日志）三张核心表。

提供的 5 个分析维度：
  GET  /api/v1/admin/analytics/overview    — 概览统计：总会话数、活跃会话数、消息数、平均消息数、安全拦截率
  GET  /api/v1/admin/analytics/trends      — 按天趋势：每天的新增会话数、消息数、推荐次数的时间序列
  GET  /api/v1/admin/analytics/intents     — Intent 分布：用户意图的分类统计（如"购药咨询"、"症状查询"等）
  GET  /api/v1/admin/analytics/conversion  — 转化漏斗：从会话创建 → 症状描述 → 推荐生成 → AI回复 的逐层转化情况
  GET  /api/v1/admin/analytics/top-drugs   — Top 推荐药品：被推荐次数最多的药品排名（从 state_snapshot 中提取）
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.sql import case

from app.db.database import get_db
from app.db.models import Message, SafetyLog, Session as SessionModel

router = APIRouter(prefix="/analytics", tags=["admin"])


# ── Schema ──────────────────────────────────────────────────
# 以下 Pydantic 模型定义了各个分析接口的响应数据结构


class OverviewOut(BaseModel):
    """概览统计响应模型 — 汇总指定时间范围内的核心运营指标。"""
    total_sessions: int = 0          # 总会话数：时间范围内创建的所有会话
    active_sessions: int = 0         # 活跃会话数：当前状态为 "active" 的会话
    total_messages: int = 0          # 总消息数：时间范围内所有用户和 AI 的消息
    avg_messages_per_session: float = 0.0  # 平均每会话消息数：反映用户交互深度
    safety_block_rate: float = 0.0   # 安全拦截率：被 BLOCK 的安全检查占总安全检查的比例


class TrendItem(BaseModel):
    """单日趋势数据项 — 表示某一天的用户行为统计。"""
    date: str               # 日期，格式为 YYYY-MM-DD（如 "2026-07-07"）
    sessions: int           # 当天新建的会话数
    messages: int           # 当天产生的消息总数
    recommendations: int    # 当天产生推荐结果的会话数


class IntentItem(BaseModel):
    """用户意图分布项 — 某一种意图及其出现次数。"""
    intent: str   # 意图名称（如 "symptom_inquiry"、"drug_purchase" 等）
    count: int    # 该意图出现的次数（按用户消息统计）


class ConversionFunnel(BaseModel):
    """转化漏斗响应模型 — 展示用户从进入系统到完成完整对话的各阶段转化情况。"""
    total_sessions: int            # 总会话数：漏斗的最顶层
    with_symptoms: int             # 有症状描述的会话数（至少包含一条用户消息）
    recommendations_given: int     # 有推荐结果的会话数（state_snapshot 不为空）
    with_ai_response: int          # 有 AI 回复的会话数（至少包含一条 assistant 消息）


class TopDrugItem(BaseModel):
    """热门药品排名项 — 单个药品的推荐次数。"""
    drug_name: str   # 药品通用名（generic_name）
    count: int       # 该药品被推荐的次数


# ── Routes ──────────────────────────────────────────────────


@router.get("/overview")
async def analytics_overview(
    request: Request,
    days: int = Query(default=30, ge=1, le=365),
) -> OverviewOut:
    """
    整体概览统计 — 汇总指定时间范围内的核心运营指标。

    这一步的作用：
        从 Session、Message、SafetyLog 三张表中统计总会话数、活跃会话数、
        总消息数、平均每会话消息数和安全拦截率，为管理员提供系统运行状态的一览视图。

    参数:
        request: FastAPI 的 Request 对象，由框架自动注入，用于获取请求上下文。
        days:    统计的时间范围天数，默认 30 天，范围 1~365 天。

    返回:
        OverviewOut: 包含 total_sessions（总会话数）、active_sessions（活跃会话数）、
                     total_messages（总消息数）、avg_messages_per_session（平均消息数）、
                     safety_block_rate（安全拦截率）的概览统计对象。
    """
    async with get_db() as db:
        # 计算统计起始时间：当前 UTC 时间往前推 N 天
        since = datetime.now(timezone.utc) - timedelta(days=days)

        # ---- 1. 查询总会话数 ----
        # 统计在时间范围内创建的所有会话
        total_sess = (
            await db.execute(
                select(func.count(SessionModel.id))
                .where(SessionModel.created_at >= since)
            )
        ).scalar() or 0

        # ---- 2. 查询活跃会话数 ----
        # 统计状态为 "active" 且在时间范围内创建的会话（当前正在进行的对话）
        active_sess = (
            await db.execute(
                select(func.count(SessionModel.id))
                .where(SessionModel.status == "active")
                .where(SessionModel.created_at >= since)
            )
        ).scalar() or 0

        # ---- 3. 查询总消息数 ----
        # 统计时间范围内所有消息（包括 user 和 assistant 消息）
        total_msgs = (
            await db.execute(
                select(func.count(Message.id))
                .where(Message.created_at >= since)
            )
        ).scalar() or 0

        # ---- 4. 计算平均每会话消息数 ----
        # 总消息数 / 总会话数，保留 1 位小数；无会话时返回 0.0
        avg_msgs = round(total_msgs / total_sess, 1) if total_sess > 0 else 0.0

        # ---- 5. 计算安全拦截率 ----
        # 先查出总安全检查次数，再查出被拦截（BLOCK）的次数
        total_safety = (
            await db.execute(
                select(func.count(SafetyLog.id))
                .where(SafetyLog.created_at >= since)
            )
        ).scalar() or 0
        blocks = (
            await db.execute(
                select(func.count(SafetyLog.id))
                .where(SafetyLog.verdict == "BLOCK")
                .where(SafetyLog.created_at >= since)
            )
        ).scalar() or 0
        # 拦截率 = 拦截次数 / 总检查次数，保留 4 位小数；无检查记录时返回 0.0
        block_rate = round(blocks / total_safety, 4) if total_safety > 0 else 0.0

        # 将统计结果封装为 OverviewOut 响应模型返回
        return OverviewOut(
            total_sessions=total_sess,
            active_sessions=active_sess,
            total_messages=total_msgs,
            avg_messages_per_session=avg_msgs,
            safety_block_rate=block_rate,
        )


@router.get("/trends")
async def analytics_trends(
    request: Request,
    days: int = Query(default=30, ge=1, le=365),
) -> list[TrendItem]:
    """
    按天趋势统计 — 返回指定时间范围内每一天的会话数、消息数和推荐数的时间序列。

    这一步的作用：
        分别从 Session 表和 Message 表中按天聚合数据，再合并构建连续的时间序列，
        即使某天没有任何数据也会返回该天的条目（各项为 0），便于前端绘制完整的趋势折线图。

    参数:
        request: FastAPI 的 Request 对象，由框架自动注入。
        days:    统计的天数，默认 30 天，范围 1~365 天。

    返回:
        list[TrendItem]: 一个按日期升序排列的列表，每个元素包含 date（日期字符串）、
                         sessions（当天会话数）、messages（当天消息数）、
                         recommendations（当天有推荐结果的会话数）。
    """
    async with get_db() as db:
        # 计算统计起始时间
        since = datetime.now(timezone.utc) - timedelta(days=days)

        # ---- 1. 查询每日会话数 ----
        # 按 Session 创建日期分组统计，结果如 [("2026-07-01", 15), ("2026-07-02", 23), ...]
        sess_trend = (
            await db.execute(
                select(
                    func.date(SessionModel.created_at).label("day"),
                    func.count(SessionModel.id).label("sessions"),
                )
                .where(SessionModel.created_at >= since)
                .group_by("day")
            )
        ).all()
        # 将查询结果转为 dict，key 为日期字符串，value 为会话数，方便后续按日期查找
        sess_map = {str(r[0]): r[1] for r in sess_trend}

        # ---- 2. 查询每日消息数 ----
        # 按消息创建日期分组统计所有消息（包含 user 和 assistant）
        msg_trend = (
            await db.execute(
                select(
                    func.date(Message.created_at).label("day"),
                    func.count(Message.id).label("messages"),
                )
                .where(Message.created_at >= since)
                .group_by("day")
            )
        ).all()
        # 转为 dict，key 为日期字符串，value 为消息数
        msg_map = {str(r[0]): r[1] for r in msg_trend}

        # ---- 3. 查询每日推荐数 ----
        # 统计 state_snapshot 不为空的会话数（state_snapshot 中有数据即表示该会话产生了推荐结果）
        rec_trend = (
            await db.execute(
                select(
                    func.date(SessionModel.created_at).label("day"),
                    func.count(SessionModel.id),
                )
                .where(SessionModel.created_at >= since)
                .where(
                    SessionModel.state_snapshot.isnot(None)
                )
                .group_by("day")
            )
        ).all()
        # 转为 dict，key 为日期字符串，value 为推荐会话数
        rec_map = {str(r[0]): r[1] for r in rec_trend}

        # ---- 4. 构建时间序列 ----
        # 从 days 天前到今天，逐天构建 TrendItem，保证日期连续无间断
        # 对于没有数据的日期，各项值默认为 0
        items = []
        for i in range(days):
            # 从最远一天（days-1 天前）开始向今天推进
            d = (datetime.now(timezone.utc) - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
            items.append(
                TrendItem(
                    date=d,
                    sessions=sess_map.get(d, 0),          # 当天会话数，无数据则为 0
                    messages=msg_map.get(d, 0),           # 当天消息数，无数据则为 0
                    recommendations=rec_map.get(d, 0),    # 当天推荐数，无数据则为 0
                )
            )

        return items


@router.get("/intents")
async def analytics_intents(
    request: Request,
    days: int = Query(default=30, ge=1, le=365),
) -> list[IntentItem]:
    """
    用户意图（Intent）分布统计 — 统计各类用户意图的出现频次。

    这一步的作用：
        从 Message 表中筛选 role="user" 且 intent 字段不为空的记录，按 intent 值分组统计，
        帮助运营人员了解用户主要的使用目的（如症状查询、购药咨询、药品对比等）。

    参数:
        request: FastAPI 的 Request 对象，由框架自动注入。
        days:    统计的时间范围天数，默认 30 天，范围 1~365 天。

    返回:
        list[IntentItem]: 按出现次数降序排列的意图列表，每个元素包含 intent（意图名称）
                          和 count（该意图出现的次数）。
    """
    async with get_db() as db:
        # 计算统计起始时间
        since = datetime.now(timezone.utc) - timedelta(days=days)

        # 按用户消息的 intent 字段分组统计，排除 intent 为空的记录
        # 结果按出现次数从高到低排序
        rows = (
            await db.execute(
                select(
                    Message.intent,             # 意图名称（分组键）
                    func.count(Message.id),     # 该意图的出现次数（聚合值）
                )
                .where(Message.role == "user")            # 只看用户发送的消息
                .where(Message.intent.isnot(None))         # 排除 intent 为 NULL 的记录
                .where(Message.created_at >= since)         # 限制时间范围
                .group_by(Message.intent)                   # 按意图分组
                .order_by(func.count(Message.id).desc())    # 按次数降序排列，高频意图在前
            )
        ).all()

        # 将查询结果转换为 IntentItem 列表返回
        return [IntentItem(intent=r[0], count=r[1]) for r in rows]


@router.get("/conversion")
async def analytics_conversion(
    request: Request,
    days: int = Query(default=30, ge=1, le=365),
) -> ConversionFunnel:
    """
    转化漏斗分析 — 统计用户从进入系统到完成完整对话的各阶段转化情况。

    这一步的作用：
        构建四个层级的漏斗指标，帮助运营人员了解用户行为路径中的流失节点：
        总会话 → 有症状描述 → 有推荐结果 → 有AI回复。通过对比各层级数量，
        可以发现用户在哪个环节流失最多，从而优化产品流程。

    参数:
        request: FastAPI 的 Request 对象，由框架自动注入。
        days:    统计的时间范围天数，默认 30 天，范围 1~365 天。

    返回:
        ConversionFunnel: 包含 total_sessions（总会话数）、
                          with_symptoms（有症状描述的会话数）、
                          recommendations_given（有推荐结果的会话数）、
                          with_ai_response（有 AI 回复的会话数）的漏斗数据。
    """
    async with get_db() as db:
        # 计算统计起始时间
        since = datetime.now(timezone.utc) - timedelta(days=days)

        # ---- 漏斗第 1 层：总会话数 ----
        # 统计时间范围内所有创建的会话
        total = (
            await db.execute(
                select(func.count(SessionModel.id))
                .where(SessionModel.created_at >= since)
            )
        ).scalar() or 0

        # ---- 漏斗第 2 层：有症状描述的会话数 ----
        # 与 Message 表关联，找出至少包含 1 条用户消息的会话（用户消息即包含症状描述）
        # 使用 distinct 避免因同一会话有多条用户消息而重复计数
        with_symptoms = (
            await db.execute(
                select(func.count(func.distinct(SessionModel.id)))
                .join(Message, SessionModel.id == Message.session_id)
                .where(Message.role == "user")
                .where(SessionModel.created_at >= since)
            )
        ).scalar() or 0

        # ---- 漏斗第 3 层：有推荐结果的会话数 ----
        # state_snapshot 不为 NULL 表示该会话产生了药品推荐结果
        rec_given = (
            await db.execute(
                select(func.count(SessionModel.id))
                .where(SessionModel.state_snapshot.isnot(None))
                .where(SessionModel.created_at >= since)
            )
        ).scalar() or 0

        # ---- 漏斗第 4 层：有 AI 回复的会话数 ----
        # 与 Message 表关联，找出至少包含 1 条 assistant 消息的会话
        # 这表示 AI 已经对用户的问题做出了实质性回复
        with_ai_response = (
            await db.execute(
                select(func.count(func.distinct(SessionModel.id)))
                .join(Message, SessionModel.id == Message.session_id)
                .where(Message.role == "assistant")
                .where(SessionModel.created_at >= since)
            )
        ).scalar() or 0

        # 将四层漏斗数据封装为 ConversionFunnel 响应模型返回
        return ConversionFunnel(
            total_sessions=total,
            with_symptoms=with_symptoms,
            recommendations_given=rec_given,
            with_ai_response=with_ai_response,
        )


@router.get("/top-drugs")
async def analytics_top_drugs(
    request: Request,
    days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=10, ge=1, le=50),
) -> list[TopDrugItem]:
    """
    热门药品 Top N 排名 — 统计被推荐次数最多的药品。

    这一步的作用：
        从 Session 表的 state_snapshot JSON 字段中提取每个会话的推荐药品列表，
        在 Python 中聚合计数后按推荐次数降序排列，帮助运营人员了解哪些药品最常被推荐给用户。

        注意：state_snapshot 是一个 JSON 字段，其内部结构为
        {"recommendations": [{"generic_name": "...", ...}, ...]}，
        由于 SQL 层面解析嵌套 JSON 比较复杂，这里选择取出所有 snapshot 后在 Python 中解析和聚合。

    参数:
        request: FastAPI 的 Request 对象，由框架自动注入。
        days:    统计的时间范围天数，默认 30 天，范围 1~365 天。
        limit:   返回的药品数量上限，默认 10 个，范围 1~50 个。

    返回:
        list[TopDrugItem]: 按推荐次数降序排列的药品列表，每个元素包含 drug_name（药品通用名）
                          和 count（该药品被推荐的次数）。
    """
    async with get_db() as db:
        # 计算统计起始时间
        since = datetime.now(timezone.utc) - timedelta(days=days)

        # ---- 1. 从数据库获取所有有推荐结果的 state_snapshot ----
        # 只查询 state_snapshot 不为 NULL 且在时间范围内的记录
        # scalars().all() 返回 JSON 字段的 Python 对象列表（list/dict）
        rows = (
            await db.execute(
                select(SessionModel.state_snapshot)
                .where(SessionModel.state_snapshot.isnot(None))
                .where(SessionModel.created_at >= since)
            )
        ).scalars().all()

        # ---- 2. 在 Python 中解析 JSON 并聚合药品推荐次数 ----
        # state_snapshot 是 JSON 字段，SQL 内解析嵌套 JSON 比较复杂，
        # 因此采用 Python 端解析的方式，使用 Counter 进行高效计数
        from collections import Counter

        counter: Counter = Counter()
        for snap in rows:
            # 确保 snapshot 是 dict 类型（避免非 JSON 数据的边界情况）
            if isinstance(snap, dict):
                # 提取 recommendations 列表，若字段不存在则返回空列表
                recs = snap.get("recommendations", [])
                for r in recs:
                    # 确保推荐项是 dict 类型
                    if isinstance(r, dict):
                        # 提取药品通用名（generic_name），空字符串跳过
                        name = r.get("generic_name", "")
                        if name:
                            counter[name] += 1

        # ---- 3. 取前 N 名返回 ----
        # most_common(limit) 按计数降序返回前 limit 个元素
        return [
            TopDrugItem(drug_name=name, count=count)
            for name, count in counter.most_common(limit)
        ]
