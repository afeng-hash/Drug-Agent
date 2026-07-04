# 知识图谱集成 (Neo4j Knowledge Graph) Plan

## 架构概览

```
                      ┌─────────────────────────────┐
                      │      recommend_node          │
                      │  (orchestrator, unchanged)   │
                      └────────────┬────────────────┘
                                   │
          ┌────────────────────────┼────────────────────────┐
          │                        │                        │
          ▼                        ▼                        ▼
  ┌───────────────┐     ┌─────────────────┐     ┌─────────────────┐
  │  Neo4j KG     │     │  Safety Rules   │     │    Scorer       │
  │  (新)         │     │  Engine (已有)  │     │    Engine (已有) │
  │               │     │                 │     │                 │
  │ 图查询候选药  │     │ 规则裁决        │     │ Σ(w × f) 排序   │
  │ 禁忌数据查询  │     │ (图提供数据)    │     │ Top-3 + 明细    │
  │ 替代药查询    │     │                 │     │                 │
  └───────┬───────┘     └────────┬────────┘     └────────┬────────┘
          │                      │                        │
          │  候选药 + 禁忌数据    │  排除列表              │  排序结果
          ▼                      ▼                        ▼
  ┌──────────────────────────────────────────────────────────────┐
  │                    recommend_node 流程                        │
  │  1. Neo4j 图查询 → 候选列表（替代 PG ILIKE）                │
  │  2. Safety Rules Engine → 排除不安全药（图提供禁忌数据）     │
  │  3. Scorer → Top-3 + 明细（不变）                           │
  │  4. LLM → 推荐理由文案（不变）                              │
  └──────────────────────────────────────────────────────────────┘
```

**两层数据源的职责边界：**

| 层 | 存储 | 职责 |
|----|------|------|
| PostgreSQL | 事务数据 | 会话、消息、药品元数据（含完整说明书字段）、库存、权重配置 |
| Neo4j | 关系数据 | 症状层级、药品-适应症、成分归属、禁忌关系、替代/相互作用 |

两个数据源通过 `generic_name` 关联（Drug 节点名 = PG `drugs.generic_name`）。

## 核心数据结构

### 图谱实体 (Pydantic)

```python
# app/kg/schemas.py

class SymptomNode(BaseModel):
    name: str                          # 标准名（唯一），如 "头痛"
    level: int = 1                     # 1/2/3，一级最粗
    aliases: list[str] = []            # 别名，如 ["头疼", "脑壳疼"]

class DrugNode(BaseModel):
    generic_name: str                  # 如 "布洛芬"（与 PG 对应）
    otc_type: str = "甲类"
    dosage_form: str = ""

class IngredientNode(BaseModel):
    name: str                          # 如 "布洛芬"（活性成分名）

class CategoryNode(BaseModel):
    name: str                          # 如 "感冒退烧"

class ConditionNode(BaseModel):
    name: str                          # 如 "胃溃疡"

class PopulationNode(BaseModel):
    name: str                          # 如 "孕妇"
```

### 图谱关系 (Pydantic)

```python
class TreatsRelation(BaseModel):
    drug: str                          # Drug.generic_name
    symptom: str                       # Symptom.name
    strength: float = 1.0              # 0-1，强适应症 vs 弱覆盖

class ContraindicatedRelation(BaseModel):
    drug: str
    target_type: str                   # "Condition" | "Population"
    target_name: str

class SimilarToRelation(BaseModel):
    drug_a: str
    drug_b: str                        # 双向关系，只存一条

class IsARelation(BaseModel):
    child: str                         # 子症状名（更具体）
    parent: str                        # 父症状名（更泛化）
```

### 查询结果

```python
class DrugCandidate(BaseModel):
    generic_name: str
    score: float                       # Σ(symptom_weight × treats_strength × decay)
    matched_symptoms: list[str]        # 命中了哪些症状
    match_details: list[dict]          # 每对 (drug, symptom) 的明细

class ContraindicationResult(BaseModel):
    drug_name: str
    has_contraindication: bool
    matched_conditions: list[str]      # 匹配到的禁忌病症
    matched_populations: list[str]     # 匹配到的禁忌人群
    matched_allergens: list[str]       # 匹配到的过敏成分

class SimilarDrugsResult(BaseModel):
    drug_name: str
    alternatives: list[str]            # 替代药品 generic_name 列表
```

## 核心 Cypher 查询

### Q1: 症状→药品候选（F2）

```cypher
// 输入: $symptoms = [{name: "头痛", weight: 1.0}, {name: "流鼻涕", weight: 0.5}]
// 策略: IS_A 展开所有祖先 → 对每个 (drug, symptom) 取最短路径 → 加权求和
// decay: 0-hop → 1.0, 1-2 hop → 0.7

UNWIND $symptoms AS sym
MATCH (s:Symptom {name: sym.name})

// 遍历所有 IS_A 路径（0..2 跳）
MATCH path = (s)-[:IS_A*0..2]->(ancestor:Symptom)
MATCH (d:Drug)-[t:TREATS]->(ancestor)

// 对每个 (drug, symptom)，取最短路径
WITH d, sym, t, length(path) AS dist
ORDER BY dist ASC
WITH d.generic_name AS drug,
     sym.name AS matched_symptom,
     sym.weight AS symptom_weight,
     HEAD(COLLECT([dist, t.strength])) AS best

WITH drug, matched_symptom, symptom_weight,
     best[0] AS min_dist, best[1] AS strength

RETURN drug,
       SUM(strength * symptom_weight *
         CASE WHEN min_dist = 0 THEN 1.0 ELSE 0.7 END) AS total_score,
       COLLECT(DISTINCT matched_symptom) AS matched_symptoms
ORDER BY total_score DESC
```

### Q2: 禁忌病症查询（F3）

```cypher
MATCH (d:Drug {generic_name: $drug_name})-[:CONTRAINDICATED_FOR]->(c:Condition)
WHERE c.name IN $user_conditions
RETURN c.name AS matched_condition
```

### Q3: 禁忌人群查询（F3）

```cypher
MATCH (d:Drug {generic_name: $drug_name})-[:CONTRAINDICATED_FOR]->(p:Population)
WHERE p.name = $special_population
RETURN p.name AS matched_population
```

### Q4: 过敏成分查询（F3）

```cypher
MATCH (d:Drug {generic_name: $drug_name})-[:HAS_INGREDIENT]->(i:Ingredient)
WHERE i.name IN $allergies
RETURN i.name AS matched_allergen
```

### Q5: 替代药品查询（F4）

```cypher
MATCH (d:Drug {generic_name: $drug_name})-[:SIMILAR_TO]-(other:Drug)
RETURN other.generic_name AS alternative
```

### Q6: 药品完整信息查询（辅助 Safety Rules Engine）

```cypher
MATCH (d:Drug {generic_name: $drug_name})
OPTIONAL MATCH (d)-[:CONTRAINDICATED_FOR]->(c:Condition)
OPTIONAL MATCH (d)-[:CONTRAINDICATED_FOR]->(p:Population)
OPTIONAL MATCH (d)-[:HAS_INGREDIENT]->(i:Ingredient)
RETURN d.generic_name AS drug,
       COLLECT(DISTINCT c.name) AS contraindicated_conditions,
       COLLECT(DISTINCT p.name) AS contraindicated_populations,
       COLLECT(DISTINCT i.name) AS ingredients
```

## 模块设计

### 模块 A: Neo4jClient (`app/kg/client.py`)

**职责**：管理 Neo4j 驱动生命周期，封装底层连接

**对外接口**：
```python
class Neo4jClient:
    def __init__(self, uri: str, user: str, password: str, database: str = "neo4j")
    async def initialize(self) -> None           # 验证连接
    async def close(self) -> None                 # 关闭驱动
    async def run(self, cypher: str, params: dict) -> list[dict]  # 执行查询
    def is_available(self) -> bool                # 健康检查
```

**依赖**：`neo4j` 异步驱动

### 模块 B: DrugGraphRepository (`app/kg/repository.py`)

**职责**：所有业务 Cypher 查询的封装，不暴露 Cypher 给调用方

**对外接口**：
```python
class DrugGraphRepository:
    def __init__(self, client: Neo4jClient)

    async def find_candidates_by_symptoms(
        self, symptoms: list[dict], categories: list[str] | None
    ) -> list[DrugCandidate]
    # Q1: 症状→药品候选，含最短路径取优 + 层级衰减

    async def check_contraindications(
        self, drug_name: str,
        user_conditions: list[str],
        special_population: str | None,
        allergies: list[str],
    ) -> ContraindicationResult
    # Q2+Q3+Q4 合并（一次查询完成三个维度）

    async def get_similar_drugs(self, drug_name: str) -> list[str]
    # Q5: 替代药品

    async def get_drug_profile(self, drug_name: str) -> dict
    # Q6: 药品完整禁忌+成分（供 Safety Rules Engine 使用）
```

**依赖**：`Neo4jClient`

### 模块 C: GraphDataSync (`app/kg/sync.py`)

**职责**：从 YAML 文件读取图谱数据，批量写入 Neo4j

**对外接口**：
```python
class GraphDataSync:
    def __init__(self, client: Neo4jClient, data_dir: str)

    async def seed_all(self) -> dict
    # 全量初始化：清空 → 建约束 → 导节点 → 导关系，返回写入统计

    async def sync_drug(self, generic_name: str) -> None
    # 单药品增量同步（从 PG 读取元数据，局部更新 Neo4j 中对应 Drug 节点）
```

**依赖**：`Neo4jClient`、`DrugRepository`（只读 PG）

## 文件组织

```
drug-Agent/
├── app/
│   ├── kg/                              ← 新建 package
│   │   ├── __init__.py
│   │   ├── client.py                    — Neo4jClient
│   │   ├── repository.py               — DrugGraphRepository
│   │   ├── schemas.py                   — Pydantic 模型
│   │   └── sync.py                      — GraphDataSync
│   ├── config.py                        ← 修改：增加 Neo4j 配置
│   ├── main.py                          ← 修改：启动时初始化 Neo4jClient
│   ├── graph/nodes/
│   │   ├── recommend.py                 ← 修改：DrugGraphRepository 换 DrugRepository ILIKE
│   │   └── safety_check.py             ← 修改：图查询禁忌数据
│   └── db/repositories/
│       └── drug.py                      ← 保留：PG ILIKE 查询作为降级备用
├── data/
│   └── kg/                              ← 新建目录
│       ├── symptoms.yaml                — 症状层级（3 级 DAG）
│       ├── drugs.yaml                   — Drug 节点 + TREATS/HAS_INGREDIENT 关系
│       ├── conditions.yaml              — Condition 节点
│       ├── populations.yaml             — Population 节点
│       └── relationships.yaml           — CONTRAINDICATED_FOR/SIMILAR_TO/INTERACTS_WITH
├── data/seed.py                         ← 修改：增加 Neo4j 全量初始化步骤
├── requirements.txt                     ← 修改：增加 neo4j 依赖
└── tests/
    └── unit/
        └── test_kg/                     ← 新建目录
            ├── __init__.py
            ├── test_repository.py       — DrugGraphRepository 单测
            └── test_client.py           — Neo4jClient 单测
```

## 技术决策

| 决策点 | 选择 | 理由 |
|--------|------|------|
| Neo4j 驱动 | `neo4j` (官方 Python 异步驱动) | 官方支持、连接池内置、AsyncSession |
| 连接管理 | 连接到 `app.state`（同 LLMClient 模式） | 全应用共享一个驱动实例，FastAPI lifespan 管理生命周期 |
| 查询封装 | Repository 模式，Cypher 字符串写在方法内部 | 业务层不碰 Cypher，未来可替换存储 |
| 数据源 | YAML 文件（非嵌入式 Cypher） | 人工可读可改，版本控制友好。sync 模块负责 Cypher 生成 |
| PG 降级 | 保留 `DrugRepository.find_by_symptoms()` | Neo4j 不可用时自动回退，check `client.is_available()` |
| 测试 | 用 mock 模拟 Neo4jClient | 单测不依赖外部 Neo4j。CI 可选连接 Docker Neo4j 跑集成 |
| 索引 | 对 `Symptom.name`、`Drug.generic_name` 建 UNIQUE CONSTRAINT | Cypher MATCH 不走全扫描，保障查询性能 |
| 属性 | 图关系属性精确到 `strength`，不存非查询字段 | 查询只需 ranking 信号，不放冗余数据 |
