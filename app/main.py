"""FastAPI application entry point — OTC Drug AI Recommendation System."""

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
    """Application lifespan — startup and shutdown hooks."""
    # ── Startup ──
    settings = Settings()
    app.state.settings = settings

    # Database
    await init_db(settings)
    app.state.db_initialized = True

    # LLM Client
    llm_client = LLMClient(settings)
    app.state.llm_client = llm_client

    # Rule Engine
    rule_engine = RuleEngine()
    register_all_rules(rule_engine)
    app.state.rule_engine = rule_engine

    # Milvus + RAG Retriever
    retriever = DrugManualRetriever(settings, llm_client)
    try:
        await retriever.ensure_collection()
    except Exception:
        pass  # Milvus might not be available at startup
    app.state.retriever = retriever

    # Repository factories (per-request DB sessions)
    def drug_repo_factory():
        return _repo_context(DrugRepository)

    def inventory_repo_factory():
        return _repo_context(InventoryRepository)

    def session_repo_factory():
        return _repo_context(SessionRepository, settings.session_expire_minutes)

    def safety_log_repo_factory():
        return _repo_context(SafetyLogRepository)

    # Graph
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

    yield  # ── Application runs here ──

    # ── Shutdown ──
    await close_db()


# FastAPI app
app = FastAPI(
    title="OTC Drug AI Recommendation System",
    description="感冒退烧 OTC 药品 AI 导购系统 — MVP",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS — allow all origins in development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routes
app.include_router(health_router)
app.include_router(session_router)
app.include_router(chat_router)


# ── Repository context helper ──
import asyncio


class _RepoContext:
    """Async context manager that wraps a repository with a DB session."""

    def __init__(self, repo_class, *extra_args):
        self.repo_class = repo_class
        self.extra_args = extra_args
        self._db = None
        self._repo = None

    async def __aenter__(self):
        db_gen = get_db()
        self._db = await anext(db_gen)
        self._repo = self.repo_class(self._db, *self.extra_args)
        return self._repo

    async def __aexit__(self, *args):
        if self._db:
            await self._db.close()


def _repo_context(repo_class, *extra_args):
    """Create an async context manager factory for a repository."""
    def factory():
        return _RepoContext(repo_class, *extra_args)
    return factory()


# ── Run ──
if __name__ == "__main__":
    import uvicorn

    settings = Settings()
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.debug,
    )
