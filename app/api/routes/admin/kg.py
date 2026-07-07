"""
Admin 知识图谱管理 — 节点/关系查看与维护。

GET  /api/v1/admin/kg/stats         — 图谱统计
GET  /api/v1/admin/kg/nodes         — 节点搜索
GET  /api/v1/admin/kg/nodes/{id}    — 节点详情
POST /api/v1/admin/kg/nodes         — 创建节点
DELETE /api/v1/admin/kg/nodes/{id}  — 删除节点
POST /api/v1/admin/kg/relations     — 创建关系
DELETE /api/v1/admin/kg/relations/{id} — 删除关系
POST /api/v1/admin/kg/sync          — 触发 KG ↔ PG 同步
"""

import logging
import re

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from app.api.routes.admin.schemas import PaginatedResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/kg", tags=["admin"])

# ── 合法的 Neo4j 标签/关系类型名（仅允许字母、数字、下划线）──
_LABEL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# 已知的关系类型白名单
_VALID_REL_TYPES = frozenset({
    "TREATS", "CONTRAINDICATED_FOR", "HAS_INGREDIENT",
    "SIMILAR_TO", "BELONGS_TO", "HAS_SYMPTOM", "HAS_SIDE_EFFECT",
    "INTERACTS_WITH", "SUITABLE_FOR", "USED_FOR",
})
# 已知的节点标签白名单
_VALID_LABELS = frozenset({
    "Drug", "Symptom", "Condition", "Ingredient", "Population", "Category",
})


def _validate_label(label: str) -> str:
    """校验单个 Neo4j 标签名，防止 Cypher 注入。"""
    if not _LABEL_RE.match(label):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid label: '{label}'. Must match pattern: {_LABEL_RE.pattern}",
        )
    return label


def _validate_labels(labels: list[str]) -> list[str]:
    """批量校验标签名。"""
    for label in labels:
        _validate_label(label)
    return labels


def _validate_rel_type(rel_type: str) -> str:
    """校验关系类型名（白名单）。"""
    if rel_type not in _VALID_REL_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid relation type: '{rel_type}'. "
                   f"Allowed: {', '.join(sorted(_VALID_REL_TYPES))}",
        )
    return rel_type


# ── Schema ──────────────────────────────────────────────────


class KGStatsOut(BaseModel):
    total_nodes: int = 0
    total_relationships: int = 0
    node_types: dict = Field(default_factory=dict)
    relationship_types: dict = Field(default_factory=dict)
    available: bool = False


class KGNodeItem(BaseModel):
    id: str
    labels: list[str]
    properties: dict


class KGNodeCreate(BaseModel):
    labels: list[str]  # 如 ["Drug"]
    properties: dict   # 如 {"generic_name": "布洛芬"}

    @field_validator("labels")
    @classmethod
    def validate_labels(cls, v: list[str]) -> list[str]:
        for label in v:
            if not _LABEL_RE.match(label):
                raise ValueError(
                    f"Invalid label: '{label}'. Must match pattern: {_LABEL_RE.pattern}"
                )
        return v


class KGRelationCreate(BaseModel):
    from_node_id: str  # Neo4j 内部 elementId
    to_node_id: str
    type: str          # "TREATS" | "CONTRAINDICATED_FOR" | ...
    properties: dict = Field(default_factory=dict)

    @field_validator("type")
    @classmethod
    def validate_rel_type(cls, v: str) -> str:
        if v not in _VALID_REL_TYPES:
            raise ValueError(
                f"Invalid relation type: '{v}'. "
                f"Allowed: {', '.join(sorted(_VALID_REL_TYPES))}"
            )
        return v


# ── Routes ──────────────────────────────────────────────────


def _get_client(request: Request):
    """获取 Neo4j 客户端。"""
    client = getattr(request.app.state, "neo4j_client", None)
    if client is None or not client.is_available():
        raise HTTPException(
            status_code=503,
            detail="Neo4j knowledge graph is not available",
        )
    return client


@router.get("/stats")
async def kg_stats(request: Request) -> KGStatsOut:
    """获取知识图谱统计信息。"""
    client = getattr(request.app.state, "neo4j_client", None)
    if client is None or not client.is_available():
        return KGStatsOut(available=False)

    try:
        # 节点统计
        node_rows = await client.run(
            "MATCH (n) RETURN labels(n) AS labels, count(n) AS cnt"
        )
        total_nodes = sum(r["cnt"] for r in node_rows)
        node_types = {
            r["labels"][0] if r["labels"] else "Unknown": r["cnt"]
            for r in node_rows
        }

        # 关系统计
        rel_rows = await client.run(
            "MATCH ()-[r]->() RETURN type(r) AS rel_type, count(r) AS cnt"
        )
        total_rels = sum(r["cnt"] for r in rel_rows)
        rel_types = {r["rel_type"]: r["cnt"] for r in rel_rows}

        return KGStatsOut(
            total_nodes=total_nodes,
            total_relationships=total_rels,
            node_types=node_types,
            relationship_types=rel_types,
            available=True,
        )
    except Exception as e:
        logger.error("KG stats failed: %s", e)
        return KGStatsOut(available=False)


@router.get("/nodes")
async def list_nodes(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    type: str | None = Query(
        default=None,
        description="节点类型: Drug|Symptom|Condition|Ingredient|Population|Category",
    ),
    search: str | None = Query(default=None, description="搜索节点属性值"),
) -> PaginatedResponse[KGNodeItem]:
    """分页搜索知识图谱节点。"""
    client = _get_client(request)

    try:
        # 构建查询 — type 必须是已知标签
        if type and type not in _VALID_LABELS:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid node type: '{type}'. Allowed: {', '.join(sorted(_VALID_LABELS))}",
            )
        label_filter = f":{type}" if type else ""
        where_clause = ""
        params: dict = {}

        if search:
            where_clause = "WHERE n.generic_name CONTAINS $search OR n.name CONTAINS $search"
            params["search"] = search

        # 计数
        count_cypher = f"MATCH (n{label_filter}) {where_clause} RETURN count(n) AS total"
        count_result = await client.run(count_cypher, params)
        total = count_result[0]["total"] if count_result else 0

        # 分页查询
        params["skip"] = (page - 1) * page_size
        params["limit"] = page_size
        cypher = (
            f"MATCH (n{label_filter}) {where_clause} "
            "RETURN elementId(n) AS node_id, labels(n) AS labels, properties(n) AS props "
            "ORDER BY node_id SKIP $skip LIMIT $limit"
        )
        rows = await client.run(cypher, params)

        items = [
            KGNodeItem(
                id=r["node_id"],
                labels=r["labels"],
                properties=r["props"],
            )
            for r in rows
        ]

        return PaginatedResponse(
            items=items, total=total, page=page, page_size=page_size,
        )
    except Exception as e:
        logger.error("KG list nodes failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/nodes/{node_id}")
async def get_node(node_id: str, request: Request) -> dict:
    """获取单个节点详情（含关联节点和关系）。"""
    client = _get_client(request)

    try:
        # 节点本身
        node_rows = await client.run(
            "MATCH (n) WHERE elementId(n) = $node_id "
            "RETURN elementId(n) AS id, labels(n) AS labels, properties(n) AS props",
            {"node_id": node_id},
        )
        if not node_rows:
            raise HTTPException(status_code=404, detail="Node not found")

        node = node_rows[0]

        # 关联关系
        rel_rows = await client.run(
            "MATCH (n)-[r]-(other) WHERE elementId(n) = $node_id "
            "RETURN type(r) AS rel_type, elementId(r) AS rel_id, "
            "labels(other) AS other_labels, properties(other) AS other_props, "
            "elementId(other) AS other_id "
            "LIMIT 50",
            {"node_id": node_id},
        )

        relations = [
            {
                "rel_id": r["rel_id"],
                "type": r["rel_type"],
                "target_id": r["other_id"],
                "target_labels": r["other_labels"],
                "target_props": r["other_props"],
            }
            for r in rel_rows
        ]

        return {
            "id": node["id"],
            "labels": node["labels"],
            "properties": node["props"],
            "relations": relations,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("KG get node failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/nodes", status_code=201)
async def create_node(body: KGNodeCreate, request: Request) -> dict:
    """创建新节点。"""
    client = _get_client(request)
    # 二次校验 labels（防御层，Pydantic validator 已做一次）
    label_str = ":".join(_validate_labels(body.labels))

    # 校验 property keys（防止注入）
    for k in body.properties:
        if not _LABEL_RE.match(k):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid property key: '{k}'. Must match: {_LABEL_RE.pattern}",
            )

    try:
        # 构建属性（keys 已经过校验，values 用 $参数化）
        props = ", ".join(
            f"{k}: ${k}" for k in body.properties
        )
        params = body.properties

        cypher = f"CREATE (n:{label_str} {{{props}}}) RETURN elementId(n) AS id, properties(n) AS props"
        rows = await client.run(cypher, params)
        return {"id": rows[0]["id"], "properties": rows[0]["props"]}
    except Exception as e:
        logger.error("KG create node failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/nodes/{node_id}", status_code=204)
async def delete_node(node_id: str, request: Request):
    """删除节点及其所有关系。"""
    client = _get_client(request)
    try:
        await client.run(
            "MATCH (n) WHERE elementId(n) = $node_id DETACH DELETE n",
            {"node_id": node_id},
        )
    except Exception as e:
        logger.error("KG delete node failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/relations", status_code=201)
async def create_relation(body: KGRelationCreate, request: Request) -> dict:
    """创建两个节点之间的关系。"""
    client = _get_client(request)
    # 二次校验关系类型（防御层，Pydantic validator 已做一次）
    rel_type = _validate_rel_type(body.type)

    # 校验 property keys
    for k in body.properties:
        if not _LABEL_RE.match(k):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid property key: '{k}'. Must match: {_LABEL_RE.pattern}",
            )

    try:
        props_str = ""
        params: dict = {
            "from_id": body.from_node_id,
            "to_id": body.to_node_id,
        }
        if body.properties:
            prop_parts = []
            for k, v in body.properties.items():
                param_key = f"prop_{k}"
                params[param_key] = v
                prop_parts.append(f"{k}: ${param_key}")
            props_str = "{" + ", ".join(prop_parts) + "}"

        cypher = (
            f"MATCH (a), (b) "
            f"WHERE elementId(a) = $from_id AND elementId(b) = $to_id "
            f"CREATE (a)-[r:{rel_type} {props_str}]->(b) "
            f"RETURN elementId(r) AS rel_id, type(r) AS rel_type"
        )
        rows = await client.run(cypher, params)
        return {"rel_id": rows[0]["rel_id"], "type": rows[0]["rel_type"]}
    except Exception as e:
        logger.error("KG create relation failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/relations/{rel_id}", status_code=204)
async def delete_relation(rel_id: str, request: Request):
    """删除关系。"""
    client = _get_client(request)
    try:
        await client.run(
            "MATCH ()-[r]-() WHERE elementId(r) = $rel_id DELETE r",
            {"rel_id": rel_id},
        )
    except Exception as e:
        logger.error("KG delete relation failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sync")
async def kg_sync(request: Request):
    """触发知识图谱数据同步（待实现）。"""
    raise HTTPException(
        status_code=501,
        detail="KG sync not yet implemented. This endpoint will trigger full Neo4j↔PostgreSQL synchronization.",
    )
