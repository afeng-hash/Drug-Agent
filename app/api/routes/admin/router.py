"""
Admin Router — 挂载所有子模块路由到 /api/v1/admin/*。

新增子模块时，在此处 import 并 include_router。
"""

from fastapi import APIRouter

from app.api.routes.admin.analytics import router as analytics_router
from app.api.routes.admin.audit import router as audit_router
from app.api.routes.admin.config import router as config_router
from app.api.routes.admin.conversations import router as conversations_router
from app.api.routes.admin.database import router as database_router
from app.api.routes.admin.feedback import router as feedback_router
from app.api.routes.admin.kg import router as kg_router
from app.api.routes.admin.llm import router as llm_router
from app.api.routes.admin.prompts import router as prompts_router
from app.api.routes.admin.risk import router as risk_router
from app.api.routes.admin.skills import router as skills_router
from app.api.routes.admin.tools import router as tools_router
from app.api.routes.admin.tracing import router as tracing_router
from app.api.routes.admin.users import router as users_router
from app.api.routes.admin.web_search import router as web_search_router

admin_router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

# ── 子模块挂载 ──
admin_router.include_router(analytics_router)
admin_router.include_router(audit_router)
admin_router.include_router(config_router)
admin_router.include_router(conversations_router)
admin_router.include_router(database_router)
admin_router.include_router(feedback_router)
admin_router.include_router(kg_router)
admin_router.include_router(llm_router)
admin_router.include_router(prompts_router)
admin_router.include_router(risk_router)
admin_router.include_router(skills_router)
admin_router.include_router(tools_router)
admin_router.include_router(tracing_router)
admin_router.include_router(users_router)
admin_router.include_router(web_search_router)


@admin_router.get("/health")
async def admin_health():
    """Admin API 健康检查。"""
    return {"status": "ok", "service": "admin"}
