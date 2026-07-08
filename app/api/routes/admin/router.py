"""
Admin Router — 挂载所有子模块路由到 /api/v1/admin/*。

这是 admin 模块的路由聚合中心。所有子模块（用户管理、对话管理、
LLM 用量、数据库管理、知识图谱、Skill、工具、Prompt、Web Search、
高风险关键字、反馈、审计、系统配置、链路追踪）的路由都在此汇总。

新增子模块的步骤：
  1. 在 app/api/routes/admin/ 下创建 xxx.py
  2. 在此文件中 import 并 include_router
  3. 子模块的 prefix 由各自文件定义（如 prefix="/users"），
     最终完整路径为 /api/v1/admin/users
"""

from fastapi import APIRouter

# ── 导入所有子模块路由 ──
# 每个子模块在自己的文件中定义 router = APIRouter(prefix="/xxx")
# 在此处统一挂载到 admin_router 下
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

# ── 创建 admin 主路由 ──
# prefix="/api/v1/admin" 表示所有子路由的前缀都是 /api/v1/admin/xxx
admin_router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

# ── 子模块挂载 ──
# include_router 将子模块的路由合并到 admin_router 中
# 最终 URL = /api/v1/admin + 子模块 prefix + 端点路径
# 例如：/api/v1/admin + /users + "" → GET /api/v1/admin/users
admin_router.include_router(analytics_router)      # /api/v1/admin/analytics/*
admin_router.include_router(audit_router)           # /api/v1/admin/audit
admin_router.include_router(config_router)          # /api/v1/admin/config
admin_router.include_router(conversations_router)   # /api/v1/admin/conversations/*
admin_router.include_router(database_router)        # /api/v1/admin/database/*
admin_router.include_router(feedback_router)        # /api/v1/admin/feedback/*
admin_router.include_router(kg_router)              # /api/v1/admin/kg/*
admin_router.include_router(llm_router)             # /api/v1/admin/llm/*
admin_router.include_router(prompts_router)         # /api/v1/admin/prompts/*
admin_router.include_router(risk_router)            # /api/v1/admin/risk-*
admin_router.include_router(skills_router)          # /api/v1/admin/skills/*
admin_router.include_router(tools_router)           # /api/v1/admin/tools/*
admin_router.include_router(tracing_router)         # /api/v1/admin/traces/*
admin_router.include_router(users_router)           # /api/v1/admin/users/*
admin_router.include_router(web_search_router)      # /api/v1/admin/web-search/*


@admin_router.get("/health")
async def admin_health():
    """Admin API 健康检查。

    用途：负载均衡器或监控系统定期访问此端点确认 admin 模块正常运行。

    Returns:
        dict: {"status": "ok", "service": "admin"}
    """
    return {"status": "ok", "service": "admin"}
