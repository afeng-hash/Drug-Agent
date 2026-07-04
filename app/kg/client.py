"""
Neo4j 客户端 —— 带有连接池的异步驱动封装（async driver wrapper）。

负责管理 neo4j.AsyncGraphDatabase 驱动的生命周期。
设计为挂载到应用状态（app.state）上（与 LLMClient 采用相同模式），
以便在多个请求之间共享该客户端实例。

使用示例 (Usage):
    client = Neo4jClient("bolt://localhost:7687", "neo4j", "password")
    await client.initialize()
    rows = await client.run("MATCH (n) RETURN n LIMIT 5", {})
    await client.close()
"""

import logging
from typing import Any

from app.config import Settings

logger = logging.getLogger(__name__)


class Neo4jClient:
    """Neo4j 异步驱动封装。

     特性 (Features):
       - 延迟初始化（Lazy initialization）：驱动实例在 initialize() 方法中创建，
         而非在 __init__() 构造函数中创建。
       - 连接池（Connection pool）：由 neo4j 驱动在内部自动管理。
       - 健康检查（Health check）：is_available() 方法可反映真实的连接状态。
       - 优雅降级（Graceful degradation）：若 initialize() 初始化失败，
         仅会将内部状态 _available 设为 False，而不会抛出异常。
    """

    def __init__(
        self,
        uri: str,
        user: str,
        password: str,
        database: str = "neo4j",
    ):
        """Create a Neo4jClient (does NOT connect yet — call initialize()).

        Args:
            uri:       Bolt URI, e.g. "bolt://localhost:7687"
            user:      Neo4j username, typically "neo4j"
            password:  Neo4j password
            database:  Target database name, default "neo4j"
        """
        self._uri = uri
        self._user = user
        self._password = password
        self._database = database
        self._driver: Any = None
        self._available = False

    # ── Lifecycle ──────────────────────────────────────────

    async def initialize(self) -> None:
        """Create the driver and verify connectivity.

        Called once at application startup. If Neo4j is unreachable, sets
        _available=False and logs a warning — does NOT raise, so the app
        can still start with PG fallback.
        """
        try:
            from neo4j import AsyncGraphDatabase

            self._driver = AsyncGraphDatabase.driver(
                self._uri,
                auth=(self._user, self._password),
                max_connection_lifetime=3600,   # 1 hour
                max_connection_pool_size=10,    # reasonable for single-app
            )
            # Verify the connection works
            await self.run("RETURN 1 AS ok", {})
            self._available = True
            logger.info("Neo4j connected — %s", self._uri)
        except Exception as exc:
            self._available = False
            self._driver = None
            logger.warning(
                "Neo4j unavailable at %s — drug queries will use PG fallback. "
                "Reason: %s",
                self._uri,
                exc,
            )

    async def close(self) -> None:
        """Close the driver and release all connections.

        Called once at application shutdown.
        """
        if self._driver is not None:
            await self._driver.close()
            self._driver = None
        self._available = False
        logger.info("Neo4j driver closed")

    # ── Query ──────────────────────────────────────────────

    async def run(self, cypher: str, params: dict | None = None) -> list[dict]:
        """执行 Cypher 查询语句，并将结果以字典列表的形式返回。

            参数 (Args):
                cypher:  Cypher 查询字符串（可使用 $param 形式的参数占位符）。
                params:  参数字典，例如 {"name": "布洛芬"}。

            返回值 (Returns):
                行记录列表，每一行都是一个字典，格式为 {列名: 值}。
                若无查询结果，则返回空列表。

            异常 (Raises):
                RuntimeError: 若在 initialize() 调用之前或 close() 调用之后执行此方法时抛出。
                neo4j.exceptions.Neo4jError: 当查询本身发生错误时抛出。
        """
        if self._driver is None:
            raise RuntimeError("Neo4jClient not initialized. Call initialize() first.")

        params = params or {}
        records, _, _ = await self._driver.execute_query(
            cypher,
            parameters_=params,
            database_=self._database,
        )
        return [dict(r) for r in records]

    # ── Health ─────────────────────────────────────────────

    def is_available(self) -> bool:
        """Check whether Neo4j is connected and ready."""
        return self._available and self._driver is not None

    # ── Factory ────────────────────────────────────────────

    @classmethod
    def from_settings(cls, settings: Settings) -> "Neo4jClient":
        """Create a Neo4jClient from application Settings.

        Args:
            settings: app.config.Settings instance

        Returns:
            Configured (but not yet initialized) Neo4jClient
        """
        return cls(
            uri=settings.neo4j_uri,
            user=settings.neo4j_user,
            password=settings.neo4j_password,
            database=settings.neo4j_database,
        )
