"""
Admin 知识图谱管理 — 节点/关系查看与维护。

本模块提供知识图谱（Knowledge Graph，简称 KG）的后台管理 RESTful API，底层使用 Neo4j 图数据库存储。
管理员可以通过这些接口对知识图谱中的节点（如药品、症状、成分等）和关系（如治疗、禁忌等）进行增删查改操作。

主要功能：
- 图谱统计：查看节点和关系的总量及分类分布
- 节点管理：搜索、查看详情、创建、删除节点
- 关系管理：创建、删除节点之间的关系
- 数据同步：触发 Neo4j 与 PostgreSQL 之间的数据同步

所有写操作均包含标签/关系类型的校验（防御 Cypher 注入），属性键名同样经过正则校验，
属性值通过 Neo4j 参数化查询传递，确保安全性。

路由前缀: /api/v1/admin/kg

具体端点:
GET  /api/v1/admin/kg/stats         — 图谱统计
GET  /api/v1/admin/kg/nodes         — 节点搜索（支持分页、类型筛选、属性搜索）
GET  /api/v1/admin/kg/nodes/{id}    — 节点详情（含关联节点和关系）
POST /api/v1/admin/kg/nodes         — 创建节点
DELETE /api/v1/admin/kg/nodes/{id}  — 删除节点（级联删除其所有关系）
POST /api/v1/admin/kg/relations     — 创建关系
DELETE /api/v1/admin/kg/relations/{id} — 删除关系
POST /api/v1/admin/kg/sync          — 触发 KG ↔ PG 同步
"""

import logging  # 日志记录，用于输出错误和调试信息
import re       # 正则表达式，用于校验 Neo4j 标签名和属性键名

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from app.api.routes.admin.schemas import PaginatedResponse  # 通用分页响应模型

# 获取当前模块的日志记录器
logger = logging.getLogger(__name__)

# 创建 APIRouter 实例，路由前缀为 /kg，在 API 文档中归类于 "admin" 标签组
router = APIRouter(prefix="/kg", tags=["admin"])

# ── Neo4j 安全校验常量 ──────────────────────────────────────────
# Neo4j 的节点标签和关系类型名只能以字母或下划线开头，后跟字母、数字或下划线
# 使用正则表达式进行严格校验，防止 Cypher 注入攻击
_LABEL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# 已知的关系类型白名单
# 关系类型表示两个知识图谱节点之间的语义连接，例如"治疗(TREATS)"、"禁忌(CONTRAINDICATED_FOR)"等
# 仅允许在此白名单中的关系类型被创建，防止非法或恶意的关系类型注入
_VALID_REL_TYPES = frozenset({
    "TREATS",                   # 治疗：药品→疾病/症状，表示该药品可治疗某种疾病
    "CONTRAINDICATED_FOR",      # 禁忌：药品→人群/疾病，表示该药品对某些人群或疾病禁用
    "HAS_INGREDIENT",           # 含有成分：药品→成分，表示该药品含有某种活性成分
    "SIMILAR_TO",               # 相似于：药品↔药品，表示两种药品相似
    "BELONGS_TO",               # 属于：药品/成分→分类，表示某实体属于某个类别
    "HAS_SYMPTOM",              # 有症状：疾病→症状，表示某种疾病会表现出某些症状
    "HAS_SIDE_EFFECT",          # 有副作用：药品→症状，表示某药品可能引起某些副作用
    "INTERACTS_WITH",           # 相互作用：药品↔药品，表示两种药品之间存在相互作用
    "SUITABLE_FOR",             # 适用于：药品→人群，表示某药品适用于特定人群
    "USED_FOR",                 # 用于：药品→疾病/症状，表示某药品用于治疗某种疾病或症状
})

# 已知的节点标签（类型）白名单
# 节点标签定义了知识图谱中的实体类型，每种标签代表一类医学知识实体
_VALID_LABELS = frozenset({
    "Drug",         # 药品
    "Symptom",      # 症状
    "Condition",    # 疾病/健康状况
    "Ingredient",   # 活性成分
    "Population",   # 人群（如儿童、孕妇、老年人等）
    "Category",     # 分类（药品分类、疾病分类等）
})


def _validate_label(label: str) -> str:
    """校验单个 Neo4j 标签名或属性键名，防止 Cypher 注入攻击。

    这一步的作用：
        确保传入的标签名符合 Neo4j 命名规范（字母或下划线开头，仅包含字母、数字、下划线），
        若不符合规范则直接拒绝请求，返回 400 错误，从而防止攻击者通过构造恶意标签名
        在 Cypher 查询中注入非法代码。

    参数:
        label (str): 待校验的 Neo4j 标签名或属性键名。

    返回:
        str: 校验通过后返回原标签名字符串。

    异常:
        HTTPException(400): 当标签名不符合命名规范时抛出。
    """
    if not _LABEL_RE.match(label):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid label: '{label}'. Must match pattern: {_LABEL_RE.pattern}",
        )
    return label


def _validate_labels(labels: list[str]) -> list[str]:
    """批量校验 Neo4j 标签名列表，防止 Cypher 注入攻击。

    这一步的作用：
        遍历传入的标签列表，逐一调用 _validate_label() 进行校验，
        确保列表中的每一个标签名都符合 Neo4j 命名规范。
        用于创建节点时对 labels 字段进行二次防御校验。

    参数:
        labels (list[str]): 待校验的标签名列表。

    返回:
        list[str]: 校验通过后返回原标签列表。

    异常:
        HTTPException(400): 当列表中任一标签名不符合命名规范时抛出。
    """
    for label in labels:
        _validate_label(label)
    return labels


def _validate_rel_type(rel_type: str) -> str:
    """校验关系类型名是否在白名单中（防御层校验）。

    这一步的作用：
        检查传入的关系类型是否在预定义的白名单 _VALID_REL_TYPES 中，
        仅允许创建白名单中已知的关系类型，防止非法或恶意的关系类型被注入到 Cypher 查询中。
        此函数在路由处理函数中被调用，作为 Pydantic validator 之后的二次防御。

    参数:
        rel_type (str): 待校验的关系类型字符串，如 "TREATS"、"BELONGS_TO" 等。

    返回:
        str: 校验通过后返回原关系类型字符串。

    异常:
        HTTPException(400): 当关系类型不在白名单中时抛出，并列出所有允许的类型。
    """
    if rel_type not in _VALID_REL_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid relation type: '{rel_type}'. "
                   f"Allowed: {', '.join(sorted(_VALID_REL_TYPES))}",
        )
    return rel_type


# ── Pydantic Schema（请求/响应数据模型） ──────────────────────


class KGStatsOut(BaseModel):
    """知识图谱统计信息的响应模型。

    表示 GET /stats 端点的返回数据结构，包含节点和关系的总量及分类统计。
    """
    total_nodes: int = 0                # 知识图谱中节点的总数量
    total_relationships: int = 0       # 知识图谱中关系的总数量
    node_types: dict = Field(default_factory=dict)           # 各类型节点的分布统计，key 为标签名，value 为数量
    relationship_types: dict = Field(default_factory=dict)   # 各类型关系的分布统计，key 为关系类型名，value 为数量
    available: bool = False             # Neo4j 服务是否可用，True 表示连接正常


class KGNodeItem(BaseModel):
    """知识图谱节点的展示模型。

    表示单个节点在列表或详情中的展示结构。
    """
    id: str              # Neo4j 内部节点 ID（elementId），全局唯一标识
    labels: list[str]    # 节点的标签列表，如 ["Drug"]、["Symptom", "Condition"] 等
    properties: dict     # 节点的属性字典，包含如 generic_name、name 等业务字段


class KGNodeCreate(BaseModel):
    """创建知识图谱节点的请求模型。

    用于 POST /nodes 端点的请求体，定义创建节点所需的数据结构。
    """
    labels: list[str]  # 节点标签列表，如 ["Drug"]。每个元素必须是合法的 Neo4j 标签名
    properties: dict   # 节点属性字典，如 {"generic_name": "布洛芬", "name": "布洛芬"}

    @field_validator("labels")
    @classmethod
    def validate_labels(cls, v: list[str]) -> list[str]:
        """Pydantic 字段校验器：验证 labels 列表中的每一个标签名是否符合规范。

        这一步的作用：
            在请求数据进入路由处理函数之前，先通过 Pydantic 的 validator 进行第一道校验，
            确保每个标签名都符合 Neo4j 命名规范（字母或下划线开头，仅含字母数字下划线）。

        参数:
            v (list[str]): 传入的标签名列表。

        返回:
            list[str]: 校验通过后的标签名列表。

        异常:
            ValueError: 当列表中任一标签名不符合命名规范时抛出。
        """
        for label in v:
            if not _LABEL_RE.match(label):
                raise ValueError(
                    f"Invalid label: '{label}'. Must match pattern: {_LABEL_RE.pattern}"
                )
        return v


class KGRelationCreate(BaseModel):
    """创建知识图谱关系的请求模型。

    用于 POST /relations 端点的请求体，定义创建两个节点之间关系所需的数据结构。
    """
    from_node_id: str    # 起始节点的 Neo4j 内部 elementId
    to_node_id: str      # 目标节点的 Neo4j 内部 elementId
    type: str            # 关系类型，必须为白名单中的值，如 "TREATS"、"BELONGS_TO" 等
    properties: dict = Field(default_factory=dict)  # 关系的属性字典，可选，默认为空字典

    @field_validator("type")
    @classmethod
    def validate_rel_type(cls, v: str) -> str:
        """Pydantic 字段校验器：验证关系类型是否在白名单中。

        这一步的作用：
            在请求数据进入路由处理函数之前，先通过 Pydantic 的 validator 进行第一道校验，
            确保关系类型是在白名单 _VALID_REL_TYPES 中预定义的合法类型。

        参数:
            v (str): 传入的关系类型字符串。

        返回:
            str: 校验通过后的关系类型字符串。

        异常:
            ValueError: 当关系类型不在白名单中时抛出。
        """
        if v not in _VALID_REL_TYPES:
            raise ValueError(
                f"Invalid relation type: '{v}'. "
                f"Allowed: {', '.join(sorted(_VALID_REL_TYPES))}"
            )
        return v


# ── 路由处理函数（Routes） ────────────────────────────────────


def _get_client(request: Request):
    """从 FastAPI 应用状态中获取 Neo4j 客户端实例。

    这一步的作用：
        从 request.app.state 中取出预先初始化好的 Neo4j 数据库客户端。
        如果客户端不存在或数据库不可用，则直接返回 503 服务不可用错误，
        避免后续的数据库操作因未能获取连接而引发不可预期的异常。

    参数:
        request (Request): FastAPI 的请求对象，其中 app.state 挂载了 neo4j_client。

    返回:
        Neo4j 客户端实例（具体类型取决于项目的 Neo4j 驱动封装）。

    异常:
        HTTPException(503): 当 Neo4j 客户端不存在或数据库不可用时抛出。
    """
    client = getattr(request.app.state, "neo4j_client", None)
    if client is None or not client.is_available():
        raise HTTPException(
            status_code=503,
            detail="Neo4j knowledge graph is not available",
        )
    return client


@router.get("/stats")
async def kg_stats(request: Request) -> KGStatsOut:
    """获取知识图谱的统计信息（节点/关系总量及分类分布）。

    这一步的作用：
        查询 Neo4j 图数据库，统计所有节点的总数量及其按标签分类的分布，
        同时统计所有关系的总数量及其按类型分类的分布，并将结果聚合返回。
        如果 Neo4j 不可用，则返回 available=False 的默认响应，而非抛出异常。

    参数:
        request (Request): FastAPI 请求对象，用于获取 Neo4j 客户端。

    返回:
        KGStatsOut: 包含节点总数、关系总数、节点类型分布、关系类型分布及可用性标志的响应模型。
    """
    # 获取 Neo4j 客户端，若不可用则直接返回空统计（不抛异常，保证接口容错性）
    client = getattr(request.app.state, "neo4j_client", None)
    if client is None or not client.is_available():
        return KGStatsOut(available=False)

    try:
        # ---- 节点统计 ----
        # 查询所有节点，按标签分组统计每个标签下的节点数量
        node_rows = await client.run(
            "MATCH (n) RETURN labels(n) AS labels, count(n) AS cnt"
        )
        # 汇总所有节点的总数量
        total_nodes = sum(r["cnt"] for r in node_rows)
        # 构建节点类型分布字典：取第一个标签作为主类型，若节点无标签则标记为 "Unknown"
        node_types = {
            r["labels"][0] if r["labels"] else "Unknown": r["cnt"]
            for r in node_rows
        }

        # ---- 关系统计 ----
        # 查询所有关系，按关系类型分组统计每种关系类型的数量
        rel_rows = await client.run(
            "MATCH ()-[r]->() RETURN type(r) AS rel_type, count(r) AS cnt"
        )
        # 汇总所有关系的总数量
        total_rels = sum(r["cnt"] for r in rel_rows)
        # 构建关系类型分布字典
        rel_types = {r["rel_type"]: r["cnt"] for r in rel_rows}

        # 返回完整的统计信息
        return KGStatsOut(
            total_nodes=total_nodes,
            total_relationships=total_rels,
            node_types=node_types,
            relationship_types=rel_types,
            available=True,
        )
    except Exception as e:
        # 查询失败时记录错误日志并返回空统计，避免接口崩溃
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
    """分页搜索知识图谱节点，支持按类型筛选和属性值模糊搜索。

    这一步的作用：
        从 Neo4j 中分页查询节点数据，支持以下筛选方式：
        1. 按节点标签类型过滤（如仅查看 Drug 类型的节点）
        2. 按属性值模糊搜索（在 generic_name 和 name 字段中搜索匹配字符串）
        支持分页参数 page 和 page_size 控制返回数据量。

    参数:
        request (Request): FastAPI 请求对象，用于获取 Neo4j 客户端。
        page (int): 当前页码，从 1 开始，默认值为 1。
        page_size (int): 每页返回的记录数，范围 1-100，默认值为 20。
        type (str | None): 节点类型筛选条件，必须为白名单 _VALID_LABELS 中的值，默认不过滤。
        search (str | None): 搜索关键词，在节点的 generic_name 和 name 属性中做模糊匹配，默认为空。

    返回:
        PaginatedResponse[KGNodeItem]: 包含节点列表、总数、当前页和每页大小的分页响应。
    """
    client = _get_client(request)

    try:
        # ---- 校验节点类型 ----
        # 如果传入了 type 参数，必须确保其在已知标签白名单中
        if type and type not in _VALID_LABELS:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid node type: '{type}'. Allowed: {', '.join(sorted(_VALID_LABELS))}",
            )
        # 如果指定了类型，则添加 Cypher 标签过滤（如：:Drug）
        label_filter = f":{type}" if type else ""
        where_clause = ""
        params: dict = {}

        # ---- 构建搜索条件 ----
        # 如果传入了 search 参数，添加对 generic_name 和 name 字段的模糊匹配
        if search:
            where_clause = "WHERE n.generic_name CONTAINS $search OR n.name CONTAINS $search"
            params["search"] = search

        # ---- 查询总数（用于前端分页组件） ----
        count_cypher = f"MATCH (n{label_filter}) {where_clause} RETURN count(n) AS total"
        count_result = await client.run(count_cypher, params)
        total = count_result[0]["total"] if count_result else 0

        # ---- 分页查询节点数据 ----
        # 计算跳过的记录数（offset）
        params["skip"] = (page - 1) * page_size
        params["limit"] = page_size
        # 按 node_id 排序以保证分页结果的稳定性，使用 SKIP/LIMIT 实现分页
        cypher = (
            f"MATCH (n{label_filter}) {where_clause} "
            "RETURN elementId(n) AS node_id, labels(n) AS labels, properties(n) AS props "
            "ORDER BY node_id SKIP $skip LIMIT $limit"
        )
        rows = await client.run(cypher, params)

        # ---- 转换为 Pydantic 响应模型 ----
        items = [
            KGNodeItem(
                id=r["node_id"],
                labels=r["labels"],
                properties=r["props"],
            )
            for r in rows
        ]

        # 返回带分页信息的响应
        return PaginatedResponse(
            items=items, total=total, page=page, page_size=page_size,
        )
    except Exception as e:
        # 查询失败时记录错误日志并返回 500 错误
        logger.error("KG list nodes failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/nodes/{node_id}")
async def get_node(node_id: str, request: Request) -> dict:
    """获取单个知识图谱节点的详细信息，包括其关联节点和关系。

    这一步的作用：
        根据节点的 Neo4j elementId 查询单个节点的完整信息，包括：
        1. 节点本身的基本信息（ID、标签列表、属性字典）
        2. 与该节点相关的所有关系及其关联的目标节点信息（最多返回 50 条）
        使得管理员可以全面了解某个实体在知识图谱中的上下文关联。

    参数:
        node_id (str): 路径参数，Neo4j 节点的内部 elementId。
        request (Request): FastAPI 请求对象，用于获取 Neo4j 客户端。

    返回:
        dict: 包含节点详情和关联关系的字典，结构为：
            {
                "id": 节点ID,
                "labels": 节点标签列表,
                "properties": 节点属性字典,
                "relations": [{"rel_id": 关系ID, "type": 关系类型, "target_id": 目标节点ID,
                               "target_labels": 目标节点标签, "target_props": 目标节点属性}, ...]
            }
    """
    client = _get_client(request)

    try:
        # ---- 查询节点本身 ----
        # 通过 elementId 精确定位节点，获取其 ID、标签和属性
        node_rows = await client.run(
            "MATCH (n) WHERE elementId(n) = $node_id "
            "RETURN elementId(n) AS id, labels(n) AS labels, properties(n) AS props",
            {"node_id": node_id},
        )
        if not node_rows:
            raise HTTPException(status_code=404, detail="Node not found")

        node = node_rows[0]

        # ---- 查询关联关系 ----
        # 使用无向关系匹配 (n)-[r]-(other)，找出与当前节点相连的所有关系和目标节点
        # LIMIT 50 防止关系过多时响应过大
        rel_rows = await client.run(
            "MATCH (n)-[r]-(other) WHERE elementId(n) = $node_id "
            "RETURN type(r) AS rel_type, elementId(r) AS rel_id, "
            "labels(other) AS other_labels, properties(other) AS other_props, "
            "elementId(other) AS other_id "
            "LIMIT 50",
            {"node_id": node_id},
        )

        # ---- 组装关系数据 ----
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

        # ---- 返回完整节点详情 ----
        return {
            "id": node["id"],
            "labels": node["labels"],
            "properties": node["props"],
            "relations": relations,
        }
    except HTTPException:
        # HTTPException 需要原样抛出（如 404），不应被通用异常处理覆盖
        raise
    except Exception as e:
        logger.error("KG get node failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/nodes", status_code=201)
async def create_node(body: KGNodeCreate, request: Request) -> dict:
    """在知识图谱中创建一个新节点。

    这一步的作用：
        接收节点标签和属性数据，在 Neo4j 中创建一个新的图节点。
        创建前会进行两层安全校验：
        1. Pydantic validator（第一层）校验 labels 格式
        2. 路由处理函数中二次校验 labels 和所有 property keys（第二层/防御层）
        属性值通过 Neo4j 的参数化查询传递，避免 Cypher 注入。

    参数:
        body (KGNodeCreate): 请求体，包含节点标签列表和属性字典。
        request (Request): FastAPI 请求对象，用于获取 Neo4j 客户端。

    返回:
        dict: 新创建节点的 ID 和属性字典，状态码为 201 Created。

    异常:
        HTTPException(400): 当标签名或属性键名不符合命名规范时抛出。
        HTTPException(500): 当 Neo4j 数据库操作失败时抛出。
    """
    client = _get_client(request)
    # 二次校验 labels（防御层，Pydantic validator 已做一次校验，此处为兜底）
    # 将标签列表用冒号拼接，形成 Cypher 的多标签语法，如 "Drug:Symptom"
    label_str = ":".join(_validate_labels(body.labels))

    # 校验所有属性键名（property keys），防止通过属性键名进行注入攻击
    for k in body.properties:
        if not _LABEL_RE.match(k):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid property key: '{k}'. Must match: {_LABEL_RE.pattern}",
            )

    try:
        # ---- 构建 Cypher 语句中的属性部分 ----
        # 属性键名已经过正则校验，直接拼入 Cypher 语句（安全）
        # 属性值使用 $参数名 的方式参数化传递，由 Neo4j 驱动负责安全转义
        props = ", ".join(
            f"{k}: ${k}" for k in body.properties
        )
        params = body.properties

        # 执行节点创建，返回新节点的 elementId 和属性
        cypher = f"CREATE (n:{label_str} {{{props}}}) RETURN elementId(n) AS id, properties(n) AS props"
        rows = await client.run(cypher, params)
        return {"id": rows[0]["id"], "properties": rows[0]["props"]}
    except Exception as e:
        logger.error("KG create node failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/nodes/{node_id}", status_code=204)
async def delete_node(node_id: str, request: Request):
    """删除指定节点及其所有关联关系（级联删除）。

    这一步的作用：
        从 Neo4j 图数据库中删除指定 elementId 的节点。
        使用 DETACH DELETE 语句实现级联删除，即同时删除该节点上连接的所有关系，
        避免因节点仍有关系引用而导致删除失败。
        成功后返回 204 No Content，表示删除成功但无响应体。

    参数:
        node_id (str): 路径参数，待删除节点的 Neo4j 内部 elementId。
        request (Request): FastAPI 请求对象，用于获取 Neo4j 客户端。

    返回:
        None: 成功时返回 HTTP 204 No Content，无响应体。

    异常:
        HTTPException(500): 当 Neo4j 删除操作失败时抛出。
    """
    client = _get_client(request)
    try:
        # 使用 DETACH DELETE 级联删除节点及其所有关联关系
        await client.run(
            "MATCH (n) WHERE elementId(n) = $node_id DETACH DELETE n",
            {"node_id": node_id},
        )
    except Exception as e:
        logger.error("KG delete node failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/relations", status_code=201)
async def create_relation(body: KGRelationCreate, request: Request) -> dict:
    """在两个已有节点之间创建一条新关系。

    这一步的作用：
        在 Neo4j 图数据库中，根据起始节点 ID 和目标节点 ID 创建一条有向关系。
        创建前进行多重安全校验：
        1. 关系类型必须在白名单 _VALID_REL_TYPES 中
        2. 所有关系属性的键名必须符合 Neo4j 命名规范
        起始节点和目标节点必须已存在，否则 Neo4j 会找不到匹配节点而导致失败。

    参数:
        body (KGRelationCreate): 请求体，包含起始节点ID、目标节点ID、关系类型和可选属性。
        request (Request): FastAPI 请求对象，用于获取 Neo4j 客户端。

    返回:
        dict: 新创建关系的 ID 和类型，状态码为 201 Created。

    异常:
        HTTPException(400): 当关系类型不在白名单中或属性键名不符合命名规范时抛出。
        HTTPException(500): 当 Neo4j 数据库操作失败时抛出（如起始/目标节点不存在）。
    """
    client = _get_client(request)
    # 二次校验关系类型（防御层，Pydantic validator 已做一次校验，此处为兜底）
    rel_type = _validate_rel_type(body.type)

    # 校验所有关系属性的键名（property keys），防止注入攻击
    for k in body.properties:
        if not _LABEL_RE.match(k):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid property key: '{k}'. Must match: {_LABEL_RE.pattern}",
            )

    try:
        # ---- 构建 Cypher 语句中的属性部分 ----
        props_str = ""  # 关系属性的 Cypher 表示，若无属性则为空字符串
        params: dict = {
            "from_id": body.from_node_id,  # 起始节点 ID
            "to_id": body.to_node_id,      # 目标节点 ID
        }
        if body.properties:
            prop_parts = []
            # 遍历属性字典，为每个属性创建参数化的键值对
            # 使用 prop_ 前缀避免参数名与节点 ID 参数冲突
            for k, v in body.properties.items():
                param_key = f"prop_{k}"
                params[param_key] = v
                prop_parts.append(f"{k}: ${param_key}")
            # 组装成 Cypher 的属性字面量格式，如 {weight: $prop_weight, since: $prop_since}
            props_str = "{" + ", ".join(prop_parts) + "}"

        # ---- 通过 Cypher 创建关系 ----
        # 先 MATCH 找到起始节点(a)和目标节点(b)，再 CREATE 一条从 a 到 b 的有向关系
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
    """删除指定 ID 的关系。

    这一步的作用：
        从 Neo4j 图数据库中删除指定 elementId 的一条关系边。
        使用无向关系匹配 ()-[r]-() 定位关系，仅删除关系本身，不影响两端的节点。
        成功后返回 204 No Content，表示删除成功但无响应体。

    参数:
        rel_id (str): 路径参数，待删除关系的 Neo4j 内部 elementId。
        request (Request): FastAPI 请求对象，用于获取 Neo4j 客户端。

    返回:
        None: 成功时返回 HTTP 204 No Content，无响应体。

    异常:
        HTTPException(500): 当 Neo4j 删除操作失败时抛出。
    """
    client = _get_client(request)
    try:
        # 使用无向关系匹配找到指定 ID 的关系并删除
        # DELETE r 仅删除关系，不删除两端节点
        await client.run(
            "MATCH ()-[r]-() WHERE elementId(r) = $rel_id DELETE r",
            {"rel_id": rel_id},
        )
    except Exception as e:
        logger.error("KG delete relation failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sync")
async def kg_sync(request: Request):
    """触发知识图谱与关系型数据库之间的数据同步（功能待实现）。

    这一步的作用：
        预留的同步端点，计划用于将 Neo4j 知识图谱中的数据与 PostgreSQL 关系数据库
        进行全量或增量同步。当前功能尚未实现，直接返回 HTTP 501 Not Implemented。

    参数:
        request (Request): FastAPI 请求对象。

    返回:
        当前始终抛出 HTTPException(501)，表示该功能尚未实现。
    """
    raise HTTPException(
        status_code=501,
        detail="KG sync not yet implemented. This endpoint will trigger full Neo4j↔PostgreSQL synchronization.",
    )
