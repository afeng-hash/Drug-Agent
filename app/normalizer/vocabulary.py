"""症状词表加载器 — 从 Neo4j 加载 Symptom 节点到内存。

接口抽象：VocabularySource(ABC) → Neo4jVocabularySource
启动时 load() 一次，运行时零 Neo4j 开销。
"""

import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class SymptomEntry:
    """单个症状词条的内存表示。"""

    __slots__ = ("name", "level", "aliases", "parents")

    def __init__(self, name: str, level: int = 1, aliases: list[str] | None = None,
                 parents: list[str] | None = None):
        self.name = name
        self.level = level
        self.aliases = aliases or []
        self.parents = parents or []

    def __repr__(self) -> str:
        return (f"SymptomEntry(name={self.name!r}, level={self.level}, "
                f"aliases={self.aliases}, parents={self.parents})")


class VocabularySource(ABC):
    """词表数据源抽象接口。

    当前实现：Neo4jVocabularySource（从 Neo4j 加载）。
    未来可替换为管理后台 DB 或其他数据源。
    """

    @abstractmethod
    async def load(self) -> list[SymptomEntry]:
        """从数据源加载全部 Symptom 节点。返回 SymptomEntry 列表。"""
        ...

    @abstractmethod
    def get_by_name(self, name: str) -> SymptomEntry | None:
        """按标准名查找 SymptomsEntry。"""
        ...

    @abstractmethod
    def resolve_alias(self, alias: str) -> str | None:
        """将别名解析为标准名，如果不是别名则返回 None。"""
        ...

    @abstractmethod
    def all_names(self) -> list[str]:
        """所有标准症状名。"""
        ...

    @abstractmethod
    def all_aliases(self) -> list[str]:
        """所有别名。"""
        ...

    @abstractmethod
    def all_entries(self) -> list[SymptomEntry]:
        """所有 SymptomsEntry（供 LLM prompt 构建）。"""
        ...


class Neo4jVocabularySource(VocabularySource):
    """从 Neo4j 加载症状词表。

    启动时调用 load()，拉取全部 Symptom 节点 + IS_A 关系，
    构建 name→entry 和 alias→name 两个内存索引。
    """

    def __init__(self, neo4j_client):
        """
        Args:
            neo4j_client: Neo4jClient 实例（必须已 initialize）
        """
        self._client = neo4j_client
        self._by_name: dict[str, SymptomEntry] = {}
        self._alias_to_name: dict[str, str] = {}
        self._loaded = False

    # ── VocabularySource 接口实现 ─────────────────────────

    async def load(self) -> list[SymptomEntry]:
        """从 Neo4j 加载词表并构建内存索引。

        Returns:
            加载到的 SymptomEntry 列表

        如果 Neo4j 不可用或查询失败，仅打印错误日志，返回空列表。
        """
        if not self._client.is_available():
            logger.error("Neo4jVocabularySource.load() failed: Neo4j is unavailable")
            return []

        try:
            rows = await self._client.run(
                """
                MATCH (s:Symptom)
                OPTIONAL MATCH (s)-[:IS_A]->(parent:Symptom)
                RETURN s.name AS name,
                       s.level AS level,
                       s.aliases AS aliases,
                       COLLECT(parent.name) AS parents
                """,
                {},
            )
        except Exception as exc:
            logger.error("Neo4jVocabularySource.load() query failed: %s", exc)
            return []

        entries = []
        for row in rows:
            entry = SymptomEntry(
                name=row["name"],
                level=row.get("level", 1),
                aliases=row.get("aliases") or [],
                parents=row.get("parents") or [],
            )
            entries.append(entry)

            # 构建索引
            self._by_name[entry.name] = entry
            for alias in entry.aliases:
                self._alias_to_name[alias] = entry.name

        self._loaded = True
        logger.info("Neo4jVocabularySource loaded %d symptom entries (%d aliases)",
                     len(entries), len(self._alias_to_name))
        return entries

    def get_by_name(self, name: str) -> SymptomEntry | None:
        return self._by_name.get(name)

    def resolve_alias(self, alias: str) -> str | None:
        return self._alias_to_name.get(alias)

    def all_names(self) -> list[str]:
        return list(self._by_name.keys())

    def all_aliases(self) -> list[str]:
        return list(self._alias_to_name.keys())

    def all_entries(self) -> list[SymptomEntry]:
        return list(self._by_name.values())

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def __len__(self) -> int:
        return len(self._by_name)
