"""
Admin API 共用 Pydantic Schema — 分页、响应封装、日期筛选等。

所有 admin 子模块从此处导入通用类型，保证 API 响应格式的一致性。

提供的通用类型：
  - PaginationParams:   分页请求参数（page, page_size）
  - PaginatedResponse:  分页响应封装（items, total, page, page_size）
  - DateRangeParams:    日期范围筛选（date_from, date_to）
  - SuccessResponse:    通用成功响应
  - ErrorResponse:      通用错误响应（含字段级错误详情）
"""

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")
"""泛型类型变量 — PaginatedResponse 用此参数化 items 的具体类型"""


# ──────────────────────────────────────────────────────────────
# 分页
# ──────────────────────────────────────────────────────────────

class PaginationParams(BaseModel):
    """分页请求参数。

    用作端点 Query 参数的基类，所有列表类端点统一使用此分页格式。

    Attributes:
        page:      当前页码，从 1 开始（默认 1）
        page_size: 每页返回条数，最小 1 最大 100（默认 20）
    """
    page: int = Field(default=1, ge=1, description="页码，从 1 开始")
    page_size: int = Field(default=20, ge=1, le=100, description="每页条数，最大 100")


class PaginatedResponse(BaseModel, Generic[T]):
    """分页响应封装 — 所有列表类 API 的统一返回格式。

    Generic[T] 表示 items 的具体类型由调用方指定，例如：
      PaginatedResponse[UserListItem]  → items 为 list[UserListItem]
      PaginatedResponse[dict]          → items 为 list[dict]

    Attributes:
        items:      当前页的数据列表
        total:      符合条件的总记录数（用于前端计算总页数）
        page:       当前页码
        page_size:  每页条数
        total_pages: 总页数（计算属性，由 total / page_size 向上取整得到）
    """
    items: list[T]
    """当前页数据列表，元素类型由泛型 T 指定"""
    total: int
    """符合筛选条件的总记录数"""
    page: int
    """当前页码（从 1 开始）"""
    page_size: int
    """每页条数"""

    @property
    def total_pages(self) -> int:
        """总页数（计算属性）— 向上取整。

        Returns:
            int: 总页数。例如 total=55, page_size=20 → 3 页
        """
        if self.page_size <= 0:
            return 0
        return (self.total + self.page_size - 1) // self.page_size


# ──────────────────────────────────────────────────────────────
# 日期范围筛选
# ──────────────────────────────────────────────────────────────

class DateRangeParams(BaseModel):
    """日期范围筛选参数 — 用于按时间段过滤数据的端点。

    Attributes:
        date_from: 开始日期（含），ISO 8601 格式如 '2026-07-01'
        date_to:   结束日期（含），ISO 8601 格式如 '2026-07-07'
    """
    date_from: str | None = Field(
        default=None, description="开始日期 ISO 8601 格式，如 '2026-07-01'",
    )
    date_to: str | None = Field(
        default=None, description="结束日期 ISO 8601 格式，如 '2026-07-07'",
    )


# ──────────────────────────────────────────────────────────────
# 通用响应
# ──────────────────────────────────────────────────────────────

class SuccessResponse(BaseModel):
    """通用成功响应 — 用于不需要返回数据的写操作（如删除、激活）。

    Attributes:
        success: 始终为 True
        message: 操作结果描述
    """
    success: bool = True
    message: str = "ok"


class ErrorDetail(BaseModel):
    """字段级错误详情 — 用于 422 校验错误响应中指明具体哪个字段有问题。

    Attributes:
        field:   出错的字段名（如 'generic_name'），可为 None 表示全局错误
        message: 错误描述
    """
    field: str | None = None
    message: str


class ErrorResponse(BaseModel):
    """通用错误响应 — 所有非 2xx 响应的统一格式。

    Attributes:
        success: 始终为 False
        message: 错误概要描述
        errors:  字段级错误详情列表（可选）
    """
    success: bool = False
    message: str
    errors: list[ErrorDetail] = Field(default_factory=list)
