"""
Database connection management — async SQLAlchemy + asyncpg.

这个模块管理 PostgreSQL 的异步连接池。
应用启动时初始化引擎，运行时通过 get_db() 分发会话，关闭时释放连接。

架构：
  FastAPI lifespan → init_db()    创建引擎 + 会话工厂
  每个请求         → get_db()     从工厂获取一个 AsyncSession（上下文管理器）
  应用关闭         → close_db()   释放连接池

注意：
  - 用 @asynccontextmanager 替代旧的 yield 生成器模式，避免双重 close 导致的
    asyncpg "cannot perform operation: another operation is in progress" 错误
  - pool_size=10 表示最多同时 10 个活跃连接
  - max_overflow=20 表示超过 pool_size 后最多再开 20 个临时连接
"""

from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import Settings

# ── 全局变量（模块级别单例）──────────────────────────────────────────
# FastAPI 应用中这些是唯一的，所以用全局变量管理生命周期

engine = None
"""SQLAlchemy AsyncEngine — 底层连接池，应用启动时创建，关闭时销毁"""

async_session_factory: async_sessionmaker[AsyncSession] | None = None
"""会话工厂 — 每个 HTTP 请求从这里获取一个新的 AsyncSession"""


class Base(DeclarativeBase):
    """SQLAlchemy ORM 基类。所有模型类（Drug, Inventory, Session 等）都继承它。

    通过 Base.metadata.create_all() 自动建表。
    """
    pass


async def init_db(settings: Settings) -> None:
    """初始化数据库引擎和会话工厂。

    在 FastAPI lifespan 的 startup 阶段调用一次。

    做了三件事：
      1. 创建异步引擎（连接池，pool_size=10, max_overflow=20）
      2. 创建会话工厂（每个请求从这里获取独立会话）
      3. 自动建表（Base.metadata.create_all，生产环境应改用 Alembic 迁移）

    Args:
        settings: 应用配置，其中 database_url 格式为 "postgresql+asyncpg://user:pass@host:port/db"
    """
    global engine, async_session_factory

    engine = create_async_engine(
        settings.database_url,
        echo=settings.debug,       # debug=True 时打印所有 SQL
        pool_size=10,              # 常驻连接数
        max_overflow=20,           # 峰值额外连接数
    )

    async_session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,    # commit 后不使属性过期，避免 lazy-load 问题
    )

    # 自动建表（开发/演示用；生产环境应使用 Alembic 迁移管理）
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db() -> None:
    """关闭数据库引擎，释放所有连接。

    在 FastAPI lifespan 的 shutdown 阶段调用。
    """
    global engine
    if engine:
        await engine.dispose()
        engine = None


@asynccontextmanager
async def get_db():
    """获取一个数据库会话的异步上下文管理器。

    使用方式：
        async with get_db() as db:
            repo = SomeRepository(db)
            result = await repo.do_something()
        # 退出 with 块时自动关闭会话、归还连接到池

    注意：
      - 用 @asynccontextmanager 而非 yield generator，避免双重 close
        （async_sessionmaker 的 __aexit__ 已经会 close session，不需要手动再 close）
      - 每个 HTTP 请求应该获取独立的 db 会话
      - session 的 transaction 默认是手动提交，由 repository 层负责 commit

    Raises:
        RuntimeError: 如果 init_db() 还没被调用（发生在应用未正常启动时）
    """
    if async_session_factory is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    async with async_session_factory() as session:
        yield session
