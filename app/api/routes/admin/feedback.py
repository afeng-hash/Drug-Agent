"""
Admin 反馈管理模块 — 用户反馈查看与统计。

本模块属于后台管理系统的反馈管理子模块，提供以下能力：
1. 分页查询所有用户对药品的反馈记录（评分 + 评论）
2. 按药品维度聚合统计反馈数据（平均评分、反馈数量）

接口一览：
  GET  /api/v1/admin/feedback       — 分页查询反馈列表，支持按药品ID和评分筛选
  GET  /api/v1/admin/feedback/stats — 按药品聚合反馈评分，按反馈数量降序排列

数据来源：Feedback 表（存储用户反馈）与 Drug 表（存储药品通用名），两表通过 drug_id 关联。
"""

from datetime import datetime

from fastapi import APIRouter, Query  # APIRouter: 路由注册器; Query: 查询参数声明与校验
from pydantic import BaseModel  # Pydantic 基类，用于定义 API 响应的数据结构
from sqlalchemy import func, select  # func: SQL 聚合函数(如 avg, count); select: 构建 SQL 查询

from app.api.routes.admin.schemas import PaginatedResponse  # 通用分页响应模型
from app.db.database import get_db  # 异步数据库会话上下文管理器
from app.db.models import Feedback, Drug  # ORM 模型: Feedback(用户反馈表), Drug(药品表)

# 创建路由实例，为所有端点统一添加 /feedback 前缀，并在 OpenAPI 文档中归类为 "admin"
router = APIRouter(prefix="/feedback", tags=["admin"])


def _iso(ts: datetime | None) -> str | None:
    """将 datetime 对象转换为 ISO 8601 格式字符串。

    作用：统一时间字段的输出格式，确保 API 响应中时间字段为标准的 ISO 格式字符串。
    若传入 None 则返回 None，保证序列化时不会因 None 值报错。

    Args:
        ts: 数据库中的 datetime 字段值，可能为 None（表示该字段无值）。

    Returns:
        ISO 8601 格式的时间字符串（如 "2025-06-15T10:30:00"），若 ts 为 None 则返回 None。
    """
    return ts.isoformat() if ts else None


class FeedbackItem(BaseModel):
    """单条反馈记录的 API 响应模型。

    用于 /feedback 分页列表接口中，每条记录包含反馈的完整信息以及关联的药品名称。
    药品名称通过 drug_id 关联 Drug 表批量查询得到，而非直接存储在 Feedback 表中。
    """
    id: int  # 反馈记录的唯一主键
    session_id: str  # 产生该反馈的用户会话 ID，用于追踪用户行为链路
    drug_id: int | None  # 被评价的药品 ID，可能为空（如用户未选择具体药品）
    drug_name: str | None  # 药品通用名（从 Drug 表关联查询），drug_id 为空时此项也为空
    rating: int  # 用户评分（通常为 1-5 的整数）
    comment: str | None  # 用户文字评论内容，可为空
    created_at: str | None  # 反馈创建时间（ISO 8601 字符串格式）


class FeedbackStatsItem(BaseModel):
    """药品反馈统计的 API 响应模型。

    用于 /feedback/stats 统计接口，按药品维度聚合展示反馈数据。
    每个条目代表一种药品的反馈汇总信息。
    """
    drug_name: str  # 药品通用名（从 Drug 表关联查询），未匹配到药品时显示 "Unknown"
    avg_rating: float  # 该药品的平均评分（保留两位小数）
    feedback_count: int  # 该药品收到的反馈总条数


@router.get("")
async def list_feedback(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    drug_id: int | None = Query(default=None),
    rating: int | None = Query(default=None),
) -> PaginatedResponse[FeedbackItem]:
    """分页查询反馈列表。

    作用：从 Feedback 表中按条件筛选并分页返回用户反馈记录。
    支持按药品 ID 和评分进行可选过滤，结果按创建时间倒序排列。
    同时通过批量查询 Drug 表填充每条记录的药品名称，避免 N+1 查询问题。

    Args:
        page: 页码，从 1 开始，默认第 1 页（ge=1 确保不小于 1）。
        page_size: 每页返回的记录数，默认 20 条，范围 1~100。
        drug_id: 可选筛选条件，按药品 ID 过滤反馈记录。
        rating: 可选筛选条件，按评分过滤反馈记录。

    Returns:
        PaginatedResponse[FeedbackItem]: 分页响应对象，包含反馈条目列表(items)、
        总记录数(total)、当前页码(page)和每页大小(page_size)。
    """
    async with get_db() as db:  # 获取异步数据库会话，退出上下文时自动关闭
        # ---- 构建基础查询 ----
        base = select(Feedback)  # 从 Feedback 表查询所有字段
        if drug_id is not None:
            base = base.where(Feedback.drug_id == drug_id)  # 按药品 ID 过滤
        if rating is not None:
            base = base.where(Feedback.rating == rating)  # 按评分过滤
        base = base.order_by(Feedback.created_at.desc())  # 按创建时间倒序，最新的在前

        # ---- 统计符合条件的总记录数（用于分页） ----
        count_q = select(func.count()).select_from(base.subquery())  # 将筛选后的查询作为子查询统计行数
        total = (await db.execute(count_q)).scalar() or 0  # 执行计数查询，结果为空时默认为 0

        # ---- 计算偏移量并执行分页查询 ----
        offset = (page - 1) * page_size  # 计算本页数据的起始偏移位置
        rows = (
            await db.execute(base.offset(offset).limit(page_size))  # 跳过前 N 条，取 page_size 条
        ).scalars().all()

        # ---- 批量查询药品名称（一次查询替代 N 次，避免 N+1 问题） ----
        drug_ids = list({r.drug_id for r in rows if r.drug_id})  # 收集当前页所有非空的 drug_id 并去重
        drug_map: dict[int, str] = {}  # 构建 drug_id -> generic_name 的映射字典
        if drug_ids:
            drug_rows = (
                await db.execute(
                    select(Drug.id, Drug.generic_name).where(Drug.id.in_(drug_ids))  # 一次性批量查询所有需要的药品名称
                )
            ).all()
            drug_map = {d[0]: d[1] for d in drug_rows}  # 将查询结果转为 {drug_id: drug_name} 字典

        # ---- 组装响应数据 ----
        items = [
            FeedbackItem(
                id=r.id,  # 反馈记录 ID
                session_id=r.session_id,  # 会话 ID
                drug_id=r.drug_id,  # 药品 ID
                drug_name=drug_map.get(r.drug_id) if r.drug_id else None,  # 从映射字典获取药品名，drug_id 为空时也为空
                rating=r.rating,  # 评分
                comment=r.comment,  # 评论内容
                created_at=_iso(r.created_at),  # 将 datetime 转为 ISO 字符串
            )
            for r in rows
        ]

        # ---- 返回分页响应 ----
        return PaginatedResponse(
            items=items, total=total, page=page, page_size=page_size,
        )


@router.get("/stats")
async def feedback_stats(
    limit: int = Query(default=20, ge=1, le=100),
) -> list[FeedbackStatsItem]:
    """按药品聚合反馈评分统计。

    作用：对 Feedback 表按 drug_id 分组聚合，计算每种药品的平均评分和反馈总数，
    结果按反馈数量降序排列，返回 Top-N 条统计记录。
    适用于管理后台的药品评价概览看板。

    Args:
        limit: 返回的统计结果数量上限，默认 20 条，范围 1~100。

    Returns:
        list[FeedbackStatsItem]: 按反馈数量降序排列的药品统计列表，
        每条包含药品名称(drug_name)、平均评分(avg_rating)、反馈总数(feedback_count)。
    """
    async with get_db() as db:  # 获取异步数据库会话
        # ---- 按 drug_id 分组聚合查询 ----
        rows = (
            await db.execute(
                select(
                    Feedback.drug_id,  # 分组键：药品 ID
                    func.avg(Feedback.rating).label("avg_r"),  # 聚合：该药品的平均评分
                    func.count(Feedback.id).label("cnt"),  # 聚合：该药品的反馈总条数
                )
                .where(Feedback.drug_id.isnot(None))  # 排除 drug_id 为空的反馈记录（无归属药品的反馈不参与统计）
                .group_by(Feedback.drug_id)  # 按药品 ID 分组
                .order_by(func.count(Feedback.id).desc())  # 按反馈数量降序，热门药品排在最前
                .limit(limit)  # 限制返回条数
            )
        ).all()

        # ---- 批量查询药品名称（一次查询替代 N 次，避免 N+1 问题） ----
        drug_ids = [r[0] for r in rows]  # 提取所有 drug_id
        drug_map: dict[int, str] = {}  # 构建 drug_id -> generic_name 的映射字典
        if drug_ids:
            drug_rows = (
                await db.execute(
                    select(Drug.id, Drug.generic_name).where(Drug.id.in_(drug_ids))  # 一次性批量查询所有需要的药品名称
                )
            ).all()
            drug_map = {d[0]: d[1] for d in drug_rows}  # 将查询结果转为 {drug_id: drug_name} 字典

        # ---- 组装统计结果 ----
        items = [
            FeedbackStatsItem(
                drug_name=drug_map.get(r[0], "Unknown"),  # 从映射字典获取药品名，未匹配到则显示 "Unknown"
                avg_rating=round(r[1], 2),  # 平均评分保留两位小数
                feedback_count=r[2],  # 反馈总数
            )
            for r in rows
        ]

        return items
