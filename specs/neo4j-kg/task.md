# 知识图谱集成 (Neo4j Knowledge Graph) Tasks

## 文件清单

| 操作 | 文件 | 职责 |
|------|------|------|
| 新建 | `app/kg/__init__.py` | 公开导出 Neo4jClient, DrugGraphRepository, GraphDataSync |
| 新建 | `app/kg/schemas.py` | Pydantic 模型：DrugCandidate, ContraindicationResult 等 |
| 新建 | `app/kg/client.py` | Neo4jClient：驱动封装、连接池、健康检查 |
| 新建 | `app/kg/repository.py` | DrugGraphRepository：6 个 Cypher 查询方法 |
| 新建 | `app/kg/sync.py` | GraphDataSync：YAML → Neo4j 批量导入 |
| 新建 | `data/kg/symptoms.yaml` | 症状层级数据（3 级 DAG，IS_A 关系） |
| 新建 | `data/kg/drugs.yaml` | Drug 节点 + TREATS/HAS_INGREDIENT 关系 |
| 新建 | `data/kg/conditions.yaml` | Condition 节点 |
| 新建 | `data/kg/populations.yaml` | Population 节点 |
| 新建 | `data/kg/relationships.yaml` | CONTRAINDICATED_FOR/SIMILAR_TO/INTERACTS_WITH |
| 修改 | `app/config.py` | 增加 NEO4J_URI/USER/PASSWORD/DATABASE 配置 |
| 修改 | `requirements.txt` | 增加 `neo4j` 依赖 |
| 修改 | `app/main.py` | 启动/关闭 Neo4jClient |
| 修改 | `data/seed.py` | 增加 KG 全量初始化步骤 |
| 修改 | `app/graph/nodes/recommend.py` | 用 DrugGraphRepository 替换 DrugRepository ILIKE |
| 修改 | `app/graph/nodes/safety_check.py` | 图查询禁忌数据传给 Safety Rules Engine |
| 新建 | `tests/unit/test_kg/__init__.py` | 空文件 |
| 新建 | `tests/unit/test_kg/test_client.py` | Neo4jClient 单测 |
| 新建 | `tests/unit/test_kg/test_repository.py` | DrugGraphRepository 单测 |

---

## T1: 环境配置 + 依赖安装

**文件：** `app/config.py`, `requirements.txt`
**依赖：** 无
**步骤：**
1. `app/config.py` 的 `Settings` 中增加字段：
   - `neo4j_uri: str = "bolt://localhost:7687"`
   - `neo4j_user: str = "neo4j"`
   - `neo4j_password: str = ""`
   - `neo4j_database: str = "neo4j"`
2. `.env` 中实际值：`NEO4J_URI=bolt://localhost:7687`、`NEO4J_USER=neo4j`、`NEO4J_PASSWORD=200409210xxf`、`NEO4J_DATABASE=neo4j`
3. `requirements.txt` 增加 `neo4j>=5.26.0`
4. 执行 `pip install neo4j`

**验证：** `python -c "from app.config import Settings; s = Settings(); print(s.neo4j_uri)"`

---

## T2: 创建数据模型

**文件：** `app/kg/schemas.py`, `app/kg/__init__.py`
**依赖：** T1
**步骤：**
1. 创建 `app/kg/` 目录
2. `schemas.py` 定义实体模型：`SymptomNode`, `DrugNode`, `IngredientNode`, `CategoryNode`, `ConditionNode`, `PopulationNode`
3. `schemas.py` 定义关系模型：`TreatsRelation`, `ContraindicatedRelation`, `SimilarToRelation`, `IsARelation`
4. `schemas.py` 定义查询结果：`DrugCandidate`, `ContraindicationResult`
5. `__init__.py` 留空或导出核心类

**验证：** `python -c "from app.kg.schemas import DrugCandidate, ContraindicationResult; print('OK')"`

---

## T3: 实现 Neo4jClient

**文件：** `app/kg/client.py`
**依赖：** T2
**步骤：**
1. 实现 `Neo4jClient.__init__`：接收 uri/user/password/database，不在此创建驱动
2. 实现 `initialize()`：创建 `neo4j.AsyncGraphDatabase.driver`，执行 `RETURN 1` 验证连接，失败时设 `_available = False`
3. 实现 `run(cypher, params)`：`await session.run(cypher, params)` → 收集 records → 转 list[dict]
4. 实现 `is_available()`：返回 `self._driver is not None and self._available`
5. 实现 `close()`：`await driver.close()`

**验证：** `python -c "from app.kg.client import Neo4jClient; c = Neo4jClient('bolt://localhost:7687','neo4j','200409210xxf'); print('OK')"`

---

## T4: 实现 DrugGraphRepository

**文件：** `app/kg/repository.py`
**依赖：** T3
**步骤：**
1. 实现 `__init__(self, client: Neo4jClient)`
2. 实现 `find_candidates_by_symptoms(symptoms, categories)`：执行 Q1 Cypher（IS_A 最短路径 + decay）
3. 实现 `check_contraindications(drug_name, user_conditions, special_population, allergies)`：执行 Q2+Q3+Q4
4. 实现 `get_similar_drugs(drug_name)`：执行 Q5
5. 实现 `get_drug_profile(drug_name)`：执行 Q6
6. 每个方法先查 `client.is_available()`，不可用返回空/默认值
7. 返回结构化为 schemas 模型

**验证：** `python -c "from app.kg.repository import DrugGraphRepository; print('OK')"`

---

## T5: 实现 GraphDataSync

**文件：** `app/kg/sync.py`
**依赖：** T4
**步骤：**
1. 实现 `__init__(self, client, data_dir)`
2. 实现 `seed_all()`：
   - 清空所有节点和关系（`MATCH (n) DETACH DELETE n`）
   - 建 UNIQUE CONSTRAINT：`Symptom.name`, `Drug.generic_name`
   - 从 5 个 YAML 文件加载数据
   - 批量 UNWIND 写入节点、再写入关系
3. 实现 `sync_drug(generic_name)`：从 PG 读 Drug 元数据 → MERGE 到 Neo4j
4. 返回写入统计 `{"nodes": N, "relationships": M}`

**验证：** `python -c "from app.kg.sync import GraphDataSync; print('OK')"`

---

## T6: 编写图谱数据文件

**文件：** `data/kg/symptoms.yaml`, `drugs.yaml`, `conditions.yaml`, `populations.yaml`, `relationships.yaml`
**依赖：** 无（纯数据，可并行）
**步骤：**
1. `symptoms.yaml`：3 级层级 DAG。至少包含：
   - 一级：头痛、发热、咳嗽、流涕、咽喉痛、全身不适、鼻塞
   - 二级：偏头痛、紧张性头痛、干咳、湿咳、咽干、咽痒、发热伴寒战、鼻塞流涕等
   - 三级：太阳穴跳痛、后脑勺痛、夜间咳嗽加重、晨起清涕等
   - 每个节点 `{name, level, aliases, parents: [...]}`
2. `drugs.yaml`：12 个 Drug 节点（从 `data/drugs.json` 提取），每个含 `{generic_name, otc_type, category, ingredients, treats: [{symptom, strength}]}`
3. `conditions.yaml`：至少胃溃疡、哮喘、高血压、肝肾疾病、心脏病、甲亢、糖尿病
4. `populations.yaml`：孕妇、哺乳期、儿童、老人
5. `relationships.yaml`：CONTRAINDICATED_FOR、SIMILAR_TO、INTERACTS_WITH

**验证：** `python -c "import yaml; [yaml.safe_load(open(f'data/kg/{f}', encoding='utf-8')) for f in ['symptoms.yaml','drugs.yaml','conditions.yaml','populations.yaml','relationships.yaml']]; print('OK')"`

---

## T7: 改造 seed.py

**文件：** `data/seed.py`
**依赖：** T5, T6
**步骤：**
1. 在 seed 脚本末尾增加 KG 初始化步骤
2. 创建 Neo4jClient + GraphDataSync → `await sync.seed_all()`
3. 打印写入统计
4. Neo4j 连接失败时打印警告但不阻断 PG 的 seed 流程

**验证：** `python data/seed.py` 完成后 Neo4j Browser 中可见 12 个 Drug 节点 + 症状层级

---

## T8: 改造 main.py（应用生命周期）

**文件：** `app/main.py`
**依赖：** T3
**步骤：**
1. 在 `app.state` 上增加 `neo4j_client`
2. 在 startup/lifespan 中 `await neo4j_client.initialize()`
3. 在 shutdown 中 `await neo4j_client.close()`
4. 连接失败不抛异常，记录日志并设置 `_available = False`

**验证：** 启动服务，日志中可见 `Neo4j connected` 或 `Neo4j unavailable, using PG fallback`

---

## T9: 改造 recommend_node

**文件：** `app/graph/nodes/recommend.py`
**依赖：** T4, T8
**步骤：**
1. `recommend_node` 参数增加 `drug_graph_repo: DrugGraphRepository`
2. 替换 `await drug_repo.find_by_symptoms(...)` 为 `await drug_graph_repo.find_candidates_by_symptoms(symptoms, categories)`
3. 包装降级逻辑：`try ... except / if not client.is_available() → 走 drug_repo.find_by_symptoms()`
4. 传入 `symptoms` 时区分主诉症状（weight=1.0）和附加症状（weight=0.5）

**验证：** 现有 unit tests 和 integration tests 全部通过

---

## T10: 改造 safety_check_node

**文件：** `app/graph/nodes/safety_check.py`
**依赖：** T4, T8
**步骤：**
1. `safety_check_node` 参数增加 `drug_graph_repo: DrugGraphRepository`
2. 在调用 Safety Rules Engine 之前，先通过 `drug_graph_repo.get_drug_profile()` 查询禁忌数据
3. 将图查询结果（conditions, populations, ingredients）作为额外输入传给 Rules Engine
4. 包降级：图不可用时 Rules Engine 仍用硬编码规则正常工作

**验证：** 现有 safety flow tests 全部通过

---

## T11: 编写 Neo4jClient 单测

**文件：** `tests/unit/test_kg/test_client.py`
**依赖：** T3
**步骤：**
1. Mock `neo4j.AsyncGraphDatabase`，测试 `initialize()` 成功/失败
2. 测试 `run()` 返回正确的 list[dict]
3. 测试 `is_available()` 在 initialize 前后的状态变化
4. 测试 `close()` 调用 driver.close()

**验证：** `pytest tests/unit/test_kg/test_client.py -v` 全部通过

---

## T12: 编写 DrugGraphRepository 单测

**文件：** `tests/unit/test_kg/test_repository.py`
**依赖：** T4
**步骤：**
1. Mock Neo4jClient.run() 返回预设结果
2. 测试 `find_candidates_by_symptoms()`：验证分数计算逻辑（strength × weight × decay）
3. 测试最短路径取优（多祖先时取最短距离）
4. 测试 `check_contraindications()`：三种禁忌维度正确解析
5. 测试 `get_similar_drugs()`
6. 测试 Neo4j 不可用时返回空/默认值（降级）

**验证：** `pytest tests/unit/test_kg/test_repository.py -v` 全部通过

---

## 执行顺序

```
T1 ──→ T2 ──→ T3 ──→ T4 ──→ T5 ──→ T7 (seed)
                    │         │
                    │         └──→ T8 (main.py)
                    │
                    ├──→ T9 (recommend)
                    │
                    └──→ T10 (safety_check)

T6 (数据文件，可并行)

T3 ──→ T11 (client 单测，可并行)
T4 ──→ T12 (repository 单测，可并行)
```
