"""
Admin API 共用 Pydantic Schema — 分页、响应封装、日期筛选等。

所有 admin 子模块从此处导入通用类型。
"""

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


# ──────────────────────────────────────────────────────────────
# 分页
# ──────────────────────────────────────────────────────────────

class PaginationParams(BaseModel):
    """分页请求参数。"""
    page: int = Field(default=1, ge=1, description="页码，从 1 开始")
    page_size: int = Field(default=20, ge=1, le=100, description="每页条数，最大 100")


class PaginatedResponse(BaseModel, Generic[T]):
    """分页响应封装。"""
    items: list[T]
    total: int
    page: int
    page_size: int

    @property
    def total_pages(self) -> int:
        if self.page_size <= 0:
            return 0
        return (self.total + self.page_size - 1) // self.page_size


# ──────────────────────────────────────────────────────────────
# 日期范围筛选
# ──────────────────────────────────────────────────────────────

class DateRangeParams(BaseModel):
    """日期范围筛选参数。"""
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
    """通用成功响应。"""
    success: bool = True
    message: str = "ok"


class ErrorDetail(BaseModel):
    """字段级错误详情。"""
    field: str | None = None
    message: str


class ErrorResponse(BaseModel):
    """通用错误响应。"""
    success: bool = False
    message: str
    errors: list[ErrorDetail] = Field(default_factory=list)
