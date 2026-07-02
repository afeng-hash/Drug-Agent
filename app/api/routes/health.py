"""Health check endpoint — verifies connectivity to all dependencies."""

from fastapi import APIRouter, Request

from app.api.schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health_check(request: Request) -> HealthResponse:
    """Check connectivity to PostgreSQL, Milvus, and LLM."""
    app_state = request.app.state

    # PostgreSQL
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

    # Milvus
    milvus_status = "error"
    try:
        retriever = getattr(app_state, "retriever", None)
        if retriever:
            await retriever.ensure_collection()
            milvus_status = "ok"
    except Exception:
        milvus_status = "error"

    # LLM
    llm_status = "error"
    try:
        llm_client = getattr(app_state, "llm_client", None)
        if llm_client and llm_client.settings.llm_api_key:
            llm_status = "ok"
        elif llm_client:
            llm_status = "no_api_key"
        else:
            llm_status = "not_initialized"
    except Exception:
        llm_status = "error"

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
