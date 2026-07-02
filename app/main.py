"""
FastAPI application entry point — OTC Drug AI Recommendation System.

这是应用的主入口文件。负责：
  1. 创建 FastAPI app 实例
  2. 在 lifespan 中初始化所有后端服务（DB、LLM、规则引擎、RAG、Graph）
  3. 注册 API 路由
  4. 提供 _RepoContext 工具类（将 DB session 注入到 Repository）

启动方式：
  python -m app.main
  或
  uvicorn app.main:app --reload
"""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes.chat import router as chat_router
from app.api.routes.health import router as health_router
from app.api.routes.session import router as session_router
from app.config import Settings
from app.db.database import close_db, get_db, init_db
from app.db.repositories.drug import DrugRepository
from app.db.repositories.inventory import InventoryRepository
from app.db.repositories.safety_log import SafetyLogRepository
from app.db.repositories.session import SessionRepository
from app.graph.builder import build_graph
from app.llm.client import LLMClient
from app.rag.retriever import DrugManualRetriever
from app.rules.definitions import register_all_rules
from app.rules.engine import RuleEngine


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理 — startup 和 shutdown 钩子。

    Startup 阶段（yield 之前）：
      1. 加载配置（Settings）
      2. 初始化数据库（创建引擎 + 连接池 + 自动建表）
      3. 创建 LLM 客户端（通义千问）
      4. 注册安全规则引擎
      5. 初始化 Milvus 连接（可选，失败不阻止启动）
      6. 创建 Repository 工厂（每个请求独立的 DB session）
      7. 构建 LangGraph 状态机

    Shutdown 阶段（yield 之后）：
      1. 关闭数据库连接池
    """
    # ═══════════════════════════════════════════════
    # Startup
    # ═══════════════════════════════════════════════

    settings = Settings()
    app.state.settings = settings

    # ── 数据库 ──
    await init_db(settings)
    app.state.db_initialized = True

    # ── LLM 客户端 ──
    llm_client = LLMClient(settings)
    app.state.llm_client = llm_client

    # ── 规则引擎（注册所有安全规则） ──
    rule_engine = RuleEngine()
    register_all_rules(rule_engine)
    app.state.rule_engine = rule_engine

    # ── Milvus + RAG 检索器 ──
    retriever = DrugManualRetriever(settings, llm_client)
    try:
        await retriever.ensure_collection()
    except Exception:
        pass  # Milvus 可能没启动，不阻塞应用启动

    app.state.retriever = retriever

    # ── Repository 工厂 ──
    # 每个 Graph run 会多次调用这些工厂，每次调用都会新开一个 DB session
    # 用完即关（参见 _RepoContext），避免连接池泄露
    def drug_repo_factory():
        return _repo_context(DrugRepository)

    def inventory_repo_factory():
        return _repo_context(InventoryRepository)

    def session_repo_factory():
        return _repo_context(SessionRepository, settings.session_expire_minutes)

    def safety_log_repo_factory():
        return _repo_context(SafetyLogRepository)

    # ── 构建 LangGraph ──
    graph = build_graph(
        llm_client=llm_client,
        rule_engine=rule_engine,
        drug_repo_factory=drug_repo_factory,
        inventory_repo_factory=inventory_repo_factory,
        session_repo_factory=session_repo_factory,
        safety_log_repo_factory=safety_log_repo_factory,
        retriever=retriever,
        max_consult_rounds=settings.max_consult_rounds,
    )
    app.state.graph = graph

    yield  # ══════════ 应用运行中 ══════════

    # ═══════════════════════════════════════════════
    # Shutdown
    # ═══════════════════════════════════════════════
    await close_db()


# ── FastAPI 实例 ──────────────────────────────────────────

app = FastAPI(
    title="OTC Drug AI Recommendation System",
    description="感冒退烧 OTC 药品 AI 导购系统 — MVP",
    version="0.1.0",
    lifespan=lifespan,
)

# ── CORS（开发阶段允许所有来源） ──
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 注册路由 ──
app.include_router(health_router)     # GET /health
app.include_router(session_router)    # POST /api/v1/sessions, GET /api/v1/sessions/{id}
app.include_router(chat_router)       # POST /api/v1/chat/{id} (SSE)


# ──────────────────────────────────────────────────────────
# Repository 上下文管理器
# ──────────────────────────────────────────────────────────
# Graph 节点（recommend / explain / inventory / end）需要
# Repository 实例来操作数据库。但 Repository 需要一个 DB session。
#
# _RepoContext 的作用：
#   在每次 Graph 节点调用时：
#     1. 从连接池获取一个新 session（get_db）
#     2. 用该 session 创建 Repository 实例
#     3. 节点执行完毕后自动归还连接到池（__aexit__）
#
# 这样每个节点都有独立的短生命周期 DB session，不会互相干扰。

class _RepoContext:
    """异步上下文管理器：为 Repository 自动管理 DB session 生命周期。

    使用方式：
        factory = _repo_context(DrugRepository)
        async with factory() as drug_repo:
            drug = await drug_repo.find_by_name("布洛芬")
        # 退出 with 时自动关闭 session
    """

    def __init__(self, repo_class, *extra_args):
        """初始化。

        Args:
            repo_class:  Repository 类（DrugRepository / InventoryRepository 等）
            *extra_args: 传给 Repository 构造函数的额外参数（如 expire_minutes）
        """
        self.repo_class = repo_class
        self.extra_args = extra_args
        self._db_ctx = None   # get_db() 返回的上下文管理器
        self._db = None        # AsyncSession 实例
        self._repo = None      # Repository 实例

    async def __aenter__(self):
        """进入上下文：获取 DB session → 创建 Repository。

        Returns:
            Repository 实例
        """
        self._db_ctx = get_db()
        self._db = await self._db_ctx.__aenter__()
        self._repo = self.repo_class(self._db, *self.extra_args)
        return self._repo

    async def __aexit__(self, *args):
        """退出上下文：关闭 session，归还连接到池。

        get_db() 的 __aexit__ 会自动调用 session.close()
        （async_sessionmaker 的上下文管理器已处理），不需要手动 close。
        """
        if self._db_ctx:
            await self._db_ctx.__aexit__(*args)


def _repo_context(repo_class, *extra_args):
    """创建一个 Repository 上下文管理器工厂。

    注意返回值直接就是 _RepoContext 实例（它本身就实现了 __aenter__/__aexit__），
    所以调用方可以写：async with _repo_context(DrugRepository) as repo:

    Args:
        repo_class:  Repository 类
        *extra_args: 传给 Repository 构造函数的额外参数

    Returns:
        _RepoContext 实例（可直接用于 async with）
    """
    def factory():
        return _RepoContext(repo_class, *extra_args)
    return factory()


# ── 直接启动 ────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    settings = Settings()
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,  # 默认 0.0.0.0
        port=settings.app_port,  # 默认 8000
        reload=settings.debug,   # 开发模式自动重载
    )
