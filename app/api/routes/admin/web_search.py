"""
Admin Web Search 管理 — Tavily 搜索配置与统计。

本模块提供 Web Search（基于 Tavily API）的后台管理接口，供运维/管理员使用。
主要功能：
  1. 查看和修改 Web Search 的运行时配置（开关、超时、返回条数等）
  2. 查看搜索调用统计（总次数、成功率、平均延迟等）
  3. 查看最近的搜索调用明细
  4. 提供测试接口，直接调用 Tavily API 预览搜索结果

API 端点一览：
  GET  /api/v1/admin/web-search/config   — 获取当前配置
  PUT  /api/v1/admin/web-search/config   — 更新配置（内存热更新，不持久化）
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
    """Web Search 配置响应模型，返回当前运行时配置的快照。"""
    enabled: bool
    """是否启用 Web Search 功能，True 表示允许调用 Tavily API"""
    timeout_seconds: float
    """单次搜索请求的超时时间，单位：秒"""
    max_results: int
    """单次搜索返回的最大结果条数"""
    api_key_configured: bool
    """Tavily API Key 是否已配置，True 表示密钥已设置（不暴露密钥内容）"""


class WebSearchConfigUpdate(BaseModel):
    """Web Search 配置更新请求模型，所有字段可选，只传需要修改的字段即可。"""
    enabled: bool | None = None
    """是否启用 Web Search 功能，None 表示不修改"""
    timeout_seconds: float | None = Field(default=None, ge=0.5, le=60.0)
    """单次搜索的超时秒数，范围 0.5~60.0，None 表示不修改"""
    max_results: int | None = Field(default=None, ge=1, le=20)
    """单次搜索返回的最大结果数，范围 1~20，None 表示不修改"""


class WebSearchStatsOut(BaseModel):
    """Web Search 调用统计数据响应模型。"""
    total_calls: int = 0
    """总调用次数"""
    success_rate: float = 0.0
    """成功率，取值范围 0.0~1.0"""
    avg_latency_ms: float = 0.0
    """平均延迟，单位：毫秒"""
    avg_results: int = 0
    """平均每次返回的结果条数"""


class WebSearchCallItem(BaseModel):
    """单条 Web Search 调用记录响应模型。"""
    id: int
    """调用记录的唯一 ID"""
    session_id: str | None
    """所属会话 ID，None 表示无关联会话"""
    model: str
    """发起调用的模型名称"""
    latency_ms: float
    """本次调用的耗时，单位：毫秒"""
    success: bool
    """本次调用是否成功"""
    created_at: str | None
    """调用发生的时间戳（ISO 格式字符串），None 表示时间未知"""


# ── Routes ──────────────────────────────────────────────────


def _get_web_settings(request: Request):
    """从请求上下文中获取应用的全局设置对象，便于读取 Web Search 相关配置。

    参数:
        request (Request): FastAPI 的请求对象，其 app.state.settings 持有全局配置实例。

    返回:
        应用的全局 settings 对象，包含 web_search_enabled、web_search_timeout、
        web_search_max_results、tavily_api_key 等属性。
    """
    # 从 FastAPI app 级别的 state 中取出共享的 settings 实例
    settings = request.app.state.settings
    return settings


@router.get("/config")
async def get_web_search_config(request: Request) -> WebSearchConfigOut:
    """获取当前 Web Search 的运行时配置快照。

    参数:
        request (Request): FastAPI 请求对象，用于访问全局 settings。

    返回:
        WebSearchConfigOut: 包含是否启用、超时时间、最大结果数、API Key 是否已配置等信息。
    """
    # 获取全局 settings 对象
    settings = _get_web_settings(request)
    # 构建并返回配置响应，api_key_configured 只暴露配置状态而不泄露密钥内容
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
    """更新 Web Search 配置（内存热更新，不持久化到配置文件）。

    这是一个运行时热更新接口：修改立即生效但重启服务后会恢复到配置文件中的默认值。

    参数:
        body (WebSearchConfigUpdate): 要更新的配置字段，所有字段可选，只传需要修改的字段。
        request (Request): FastAPI 请求对象，用于访问全局 settings。

    返回:
        WebSearchConfigOut: 更新后的完整配置快照。
    """
    # 获取全局 settings 对象
    settings = _get_web_settings(request)

    # 逐个检查并更新：只修改请求体中明确传入的字段
    if body.enabled is not None:
        settings.web_search_enabled = body.enabled
    if body.timeout_seconds is not None:
        settings.web_search_timeout = body.timeout_seconds
    if body.max_results is not None:
        settings.web_search_max_results = body.max_results

    # 返回更新后的完整配置
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
    """获取 Web Search 调用统计数据。

    注意：精确的工具级统计需要 LLMCallLog 支持 tool_name 字段。
    当前返回 0 值占位。使用 /admin/llm/overview 查看 LLM 总体用量。

    参数:
        request (Request): FastAPI 请求对象。
        days (int): 统计最近几天的数据，默认 7 天，范围 1~90。

    返回:
        WebSearchStatsOut: 包含总调用次数、成功率、平均延迟、平均结果条数。
    """
    # Phase 1: tool_call 级别统计尚未支持，返回空占位。
    # Phase 2: 在 LLMCallLog 新增 tool_name 字段后，按 node="react" + tool_name="search_web" 进行聚合查询。
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
    """获取最近 Web Search 调用明细列表（分页）。

    注意：精确的工具级明细需要 LLMCallLog 支持 tool_name 字段。
    当前返回空列表占位。使用 /admin/llm/calls 查看 LLM 调用明细。

    参数:
        request (Request): FastAPI 请求对象。
        page (int): 页码，从 1 开始，默认第 1 页。
        page_size (int): 每页条数，默认 20，范围 1~100。
        days (int): 查询最近几天的数据，默认 7 天，范围 1~90。

    返回:
        PaginatedResponse[WebSearchCallItem]: 分页响应，包含调用记录列表、总数、当前页码和每页大小。
    """
    # 当前 tool_name 字段尚未支持，返回空列表占位
    return PaginatedResponse(
        items=[], total=0, page=page, page_size=page_size,
    )


@router.post("/test")
async def test_web_search(request: Request, query: str = Query(default="布洛芬 副作用")) -> dict:
    """测试 Web Search 功能，直接调用 Tavily API 并预览返回结果。

    此接口用于验证 Tavily API 配置是否正确、网络是否可达，
    以及预览实际搜索结果的内容格式。不会记录到统计中。

    参数:
        request (Request): FastAPI 请求对象，用于获取 Tavily API Key 等配置。
        query (str): 搜索关键词，默认为 "布洛芬 副作用"。

    返回:
        dict: 包含 status（"ok" 或 "error"）、query（搜索词）、
              results（搜索结果列表）或 error（异常信息）的字典。
    """
    # 获取全局 settings
    settings = _get_web_settings(request)
    # 检查 Tavily API Key 是否已配置，未配置则返回 400 错误
    if not settings.tavily_api_key:
        raise HTTPException(status_code=400, detail="Tavily API key not configured")

    try:
        # 懒加载 TavilySearchService，避免模块级别导入带来的启动依赖问题
        from app.search.service import TavilySearchService
        # 创建搜索服务实例并执行搜索
        service = TavilySearchService(settings)
        results = await service.search(query)
        # 搜索成功，返回结果
        return {"status": "ok", "query": query, "results": results}
    except Exception as e:
        # 搜索失败，返回错误信息而不抛出异常，保证接口始终返回 200
        return {"status": "error", "query": query, "error": str(e)}
