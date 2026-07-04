"""Unit tests for Neo4jClient."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.kg.client import Neo4jClient


class TestNeo4jClient:
    """Tests for Neo4jClient lifecycle and query methods."""

    @pytest.fixture
    def client(self):
        """Create an uninitialized Neo4jClient."""
        return Neo4jClient(
            uri="bolt://localhost:7687",
            user="neo4j",
            password="test",
            database="neo4j",
        )

    def test_is_available_false_before_initialize(self, client):
        """Before initialize(), is_available() should return False."""
        assert client.is_available() is False

    @pytest.mark.asyncio
    async def test_initialize_success(self, client):
        """initialize() should set _available=True on successful connection."""
        mock_driver = MagicMock()
        # Mock execute_query to return a dummy result
        mock_driver.execute_query = AsyncMock(return_value=([MagicMock()], None, None))

        with patch(
            "neo4j.AsyncGraphDatabase.driver", return_value=mock_driver
        ):
            await client.initialize()

        assert client.is_available() is True

    @pytest.mark.asyncio
    async def test_initialize_failure(self, client):
        """initialize() should set _available=False on connection failure."""
        with patch(
            "neo4j.AsyncGraphDatabase.driver",
            side_effect=Exception("Connection refused"),
        ):
            await client.initialize()

        assert client.is_available() is False

    @pytest.mark.asyncio
    async def test_run_returns_list_of_dicts(self, client):
        """run() should return a list of dicts from query results."""
        mock_driver = MagicMock()
        # Create a mock record that supports dict()
        mock_record = MagicMock()
        mock_record.__iter__ = lambda self: iter([])
        mock_record.items = lambda: [("name", "布洛芬"), ("score", 1.5)]
        mock_driver.execute_query = AsyncMock(
            return_value=([mock_record], None, None)
        )

        with patch(
            "neo4j.AsyncGraphDatabase.driver", return_value=mock_driver
        ):
            await client.initialize()
            rows = await client.run("MATCH (d:Drug) RETURN d", {})

        assert isinstance(rows, list)
        assert len(rows) == 1
        # dict(mock_record) would call items(), which we mocked
        assert isinstance(rows[0], dict)

    @pytest.mark.asyncio
    async def test_run_raises_if_not_initialized(self, client):
        """run() should raise RuntimeError if called before initialize()."""
        with pytest.raises(RuntimeError, match="not initialized"):
            await client.run("RETURN 1", {})

    @pytest.mark.asyncio
    async def test_close_sets_unavailable(self, client):
        """close() should set _available=False and _driver=None."""
        mock_driver = MagicMock()
        mock_driver.execute_query = AsyncMock(return_value=([MagicMock()], None, None))
        mock_driver.close = AsyncMock()

        with patch(
            "neo4j.AsyncGraphDatabase.driver", return_value=mock_driver
        ):
            await client.initialize()
            assert client.is_available() is True
            await client.close()

        assert client.is_available() is False

    @pytest.mark.asyncio
    async def test_from_settings(self):
        """from_settings() should read Neo4j config from Settings."""
        from app.config import Settings

        settings = Settings(
            neo4j_uri="bolt://localhost:7687",
            neo4j_user="neo4j",
            neo4j_password="secret",
            neo4j_database="mydb",
        )
        c = Neo4jClient.from_settings(settings)
        assert c._uri == "bolt://localhost:7687"
        assert c._user == "neo4j"
        assert c._password == "secret"
        assert c._database == "mydb"
