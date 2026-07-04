"""
Knowledge Graph package — Neo4j integration for drug recommendation.

Exports:
    Neo4jClient          — async driver wrapper with connection pool
    DrugGraphRepository  — Cypher query methods for business use cases
    GraphDataSync        — YAML → Neo4j bulk import
"""

# Lazy imports — modules are imported when needed, not at package load time.
# This avoids circular imports and allows incremental development.

__all__ = ["Neo4jClient", "DrugGraphRepository", "GraphDataSync"]


def __getattr__(name):
    if name == "Neo4jClient":
        from app.kg.client import Neo4jClient as _c
        return _c
    if name == "DrugGraphRepository":
        from app.kg.repository import DrugGraphRepository as _r
        return _r
    if name == "GraphDataSync":
        from app.kg.sync import GraphDataSync as _s
        return _s
    raise AttributeError(f"module 'app.kg' has no attribute {name!r}")
