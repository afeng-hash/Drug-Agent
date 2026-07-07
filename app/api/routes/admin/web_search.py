"""
Admin Web Search 管理 — Tavily 搜索配置与统计。

GET  /api/v1/admin/web-search/config   — 当前配置
PUT  /api/v1/admin/web-search/config   — 更新配置
GET  /api/v1/admin/web-search/stats    — 调用统计
GET  /api/v1/admin/web-search/calls    — 最近调用明细
POST /api/v1/admin/web-search/test     — 测试搜索
"""

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.routes.admin.schemas import PaginatedResponse
from app.db.database import get_db
from app.db.models import LLMCallLog

router = APIRouter(prefix="/web-search", tags=["admin"])


# ── Schema ──────────────────────────────────────────────────


class WebSearchConfigOut(BaseModel):
    enabled: bool
    timeout_seconds: float
    max_results: int
    api_key_configured: bool


class WebSearchConfigUpdate(BaseModel):
    enabled: bool | None = None
    timeout_seconds: float | None = Field(default=None, ge=0.5, le=60.0)
    max_results: int | None = Field(default=None, ge=1, le=20)


class WebSearchStatsOut(BaseModel):
    total_calls: int = 0
    success_rate: float = 0.0
    avg_latency_ms: float = 0.0
    avg_results: int = 0


class WebSearchCallItem(BaseModel):
    id: int
    session_id: str | None
    model: str
    latency_ms: float
    success: bool
    created_at: str | None


# ── Routes ──────────────────────────────────────────────────


def _get_web_settings(request: Request):
    """获取 Web Search 相关配置。"""
    settings = request.app.state.settings
    return settings


@router.get("/config")
async def get_web_search_config(request: Request) -> WebSearchConfigOut:
    """获取当前 Web Search 配置。"""
    settings = _get_web_settings(request)
    return WebSearchConfigOut(
        enabled=settings.web_search_enabled,
        timeout_seconds=settings.web_search_timeout,
        max_results=settings.web_search_max_results,
        api_key_configured=bool(settings.tavily_api_key),
    )


@router.put("/config")
async def update_web_search_config(
    body: WebSearchConfigUpdate, request: Request
) -> WebSearchConfigOut:
    """更新 Web Search 配置（内存热更新）。"""
    settings = _get_web_settings(request)

    if body.enabled is not None:
        settings.web_search_enabled = body.enabled
    if body.timeout_seconds is not None:
        settings.web_search_timeout = body.timeout_seconds
    if body.max_results is not None:
        settings.web_search_max_results = body.max_results

    return WebSearchConfigOut(
        enabled=settings.web_search_enabled,
        timeout_seconds=settings.web_search_timeout,
        max_results=settings.web_search_max_results,
        api_key_configured=bool(settings.tavily_api_key),
    )


@router.get("/stats")
async def web_search_stats(
    request: Request,
    days: int = Query(default=7, ge=1, le=90),
) -> WebSearchStatsOut:
    """Web Search 调用统计。

    注意：精确的工具级统计需要 LLMCallLog 支持 tool_name 字段。
    当前返回 0 值占位。使用 /admin/llm/overview 查看 LLM 总体用量。
    """
    # Phase 1: tool_call 级别统计尚未支持，返回空占位。
    # Phase 2: 在 LLMCallLog 新增 tool_name 字段后，按 node="react" + tool_name="search_web" 查询。
    return WebSearchStatsOut(
        total_calls=0,
        success_rate=0.0,
        avg_latency_ms=0.0,
        avg_results=0,
    )


@router.get("/calls")
async def web_search_calls(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    days: int = Query(default=7, ge=1, le=90),
) -> PaginatedResponse[WebSearchCallItem]:
    """最近 Web Search 调用明细。

    注意：精确的工具级明细需要 LLMCallLog 支持 tool_name 字段。
    当前返回空列表占位。使用 /admin/llm/calls 查看 LLM 调用明细。
    """
    return PaginatedResponse(
        items=[], total=0, page=page, page_size=page_size,
    )


@router.post("/test")
async def test_web_search(request: Request, query: str = Query(default="布洛芬 副作用")) -> dict:
    """测试 Web Search（直接调用 Tavily API 预览结果）。"""
    settings = _get_web_settings(request)
    if not settings.tavily_api_key:
        raise HTTPException(status_code=400, detail="Tavily API key not configured")

    try:
        from app.search.service import TavilySearchService
        service = TavilySearchService(settings)
        results = await service.search(query)
        return {"status": "ok", "query": query, "results": results}
    except Exception as e:
        return {"status": "error", "query": query, "error": str(e)}
