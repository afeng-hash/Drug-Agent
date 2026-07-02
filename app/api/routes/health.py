"""Health check endpoint — 后端服务连通性检查。

GET /health — 返回 PostgreSQL、Milvus、LLM 的连接状态。
"""

from fastapi import APIRouter, Request

from app.api.schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health_check(request: Request) -> HealthResponse:
    """检查所有后端依赖的连通性。

    用于：
      - K8s / Docker 健康探针
      - 部署后的快速验证
      - 运维监控面板

    检查项：
      1. PostgreSQL — 执行 SELECT 1
      2. Milvus      — 确保 collection 存在
      3. LLM         — 检查 API key 是否已配置

    Returns:
        HealthResponse，各字段为 "ok" 或错误状态
    """
    app_state = request.app.state

    # ── PostgreSQL 检查 ──
    postgres_status = "error"
    try:
        from app.db.database import async_session_factory
        if async_session_factory:
            async with async_session_factory() as session:
                await session.execute(
                    __import__("sqlalchemy").text("SELECT 1")
                )
            postgres_status = "ok"
    except Exception:
        postgres_status = "error"

    # ── Milvus 检查 ──
    milvus_status = "error"
    try:
        retriever = getattr(app_state, "retriever", None)
        if retriever:
            await retriever.ensure_collection()
            milvus_status = "ok"
    except Exception:
        milvus_status = "error"

    # ── LLM 检查 ──
    llm_status = "error"
    try:
        llm_client = getattr(app_state, "llm_client", None)
        if llm_client and llm_client.settings.llm_api_key:
            llm_status = "ok"
        elif llm_client:
            llm_status = "no_api_key"   # 客户端已初始化但未配置 key
        else:
            llm_status = "not_initialized"
    except Exception:
        llm_status = "error"

    # ── 整体判断：任意一个不是 ok → degraded ──
    overall = "ok" if (
        postgres_status == "ok"
        and milvus_status == "ok"
        and llm_status == "ok"
    ) else "degraded"

    return HealthResponse(
        status=overall,
        postgres=postgres_status,
        milvus=milvus_status,
        llm=llm_status,
    )
