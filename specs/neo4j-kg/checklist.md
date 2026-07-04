# 知识图谱集成 (Neo4j Knowledge Graph) Checklist

> 每一项通过运行代码或观察行为来验证，聚焦系统行为。

## 实现完整性
- [ ] `app/kg/` package 可导入（验证：`python -c "from app.kg import Neo4jClient, DrugGraphRepository, GraphDataSync"`）
- [ ] Neo4jClient 可连接本地 Neo4j 并执行查询（验证：`python -c "import asyncio; from app.kg.client import Neo4jClient; c = Neo4jClient(...); asyncio.run(c.initialize()); print(c.is_available())"`）
- [ ] DrugGraphRepository 所有 4 个方法可调用（验证：`python -c "from app.kg.repository import DrugGraphRepository; print(dir(DrugGraphRepository))"`）
- [ ] GraphDataSync 可读取 YAML 并写入 Neo4j（验证：`python -c "from app.kg.sync import GraphDataSync; print('OK')"`）
- [ ] 5 个 YAML 数据文件语法正确（验证：逐个 `yaml.safe_load` 无异常）
- [ ] `app/config.py` 包含 4 个 Neo4j 配置字段（验证：`Settings().neo4j_uri` 非空）
- [ ] `requirements.txt` 包含 `neo4j` 依赖

## 图数据验证
- [ ] seed.py 运行后 Neo4j 包含 12 个 Drug 节点（验证：Neo4j Browser `MATCH (d:Drug) RETURN count(d)` → 12）
- [ ] Symptom 节点有三级层级（验证：`MATCH (s:Symptom) WHERE s.level=1 RETURN count(s)` → ≥ 5）
- [ ] 每个一级症状至少有一个子症状（验证：`MATCH (parent:Symptom)-[:IS_A]-(child:Symptom) WHERE parent.level=1 RETURN count(DISTINCT parent)` → ≥ 3）
- [ ] 每个 Drug 至少有一条 TREATS 关系（验证：`MATCH (d:Drug)-[:TREATS]->(s:Symptom) RETURN count(DISTINCT d)` → 12）
- [ ] 至少 3 个 Drug 有 CONTRAINDICATED_FOR→Condition 关系（验证：`MATCH (d:Drug)-[:CONTRAINDICATED_FOR]->(c:Condition) RETURN count(DISTINCT d)` → ≥ 3）
- [ ] 至少 2 个 Drug 有 CONTRAINDICATED_FOR→Population 关系（验证：`MATCH (d:Drug)-[:CONTRAINDICATED_FOR]->(p:Population) RETURN count(DISTINCT d)` → ≥ 2）
- [ ] 至少 2 对 Drug 有 SIMILAR_TO 关系（验证：`MATCH (:Drug)-[:SIMILAR_TO]-(:Drug) RETURN count(*)/2` → ≥ 2）
- [ ] `[:TREATS].strength` 取值范围 0-1（验证：`MATCH ()-[t:TREATS]->() WHERE t.strength<0 OR t.strength>1 RETURN count(t)` → 0）

## 功能验证

### F2: 症状→药品图查询
- [ ] AC1: `find_candidates_by_symptoms([{name:"头痛",weight:1.0},{name:"发烧",weight:0.5}])` → 返回布洛芬、对乙酰氨基酚等（验证：单测或脚本确认候选列表不为空）
- [ ] AC2: 三级症状"偏头痛"通过 IS_A 匹配到"头痛" → 找到 TREATS 头痛的药品（验证：score > 0 且 matched_symptoms 含"偏头痛"）
- [ ] AC3: strength=0.9 的药品排在 strength=0.3 的前面（验证：候选中第一个的 total_score > 最后一个）
- [ ] AC4: 主诉症状（weight=1.0）贡献 > 附加症状（weight=0.5），同一 drug 对主诉的得分高于附加（验证：日志或单测确认）
- [ ] AC9: 症状有多个父节点时不重复计算（验证：单测确认返回结果中 drug 不重复）

### F3: 禁忌数据查询
- [ ] AC5: `get_drug_profile("布洛芬")` 返回 contraindicated_conditions 含"胃溃疡"（验证：单测或脚本确认）
- [ ] `check_contraindications("布洛芬", ["胃溃疡"], "孕妇", ["阿司匹林"])` 三维度均有匹配（验证：ContraindicationResult.has_contraindication=True）

### F4: 替代药品
- [ ] AC7: `get_similar_drugs("布洛芬")` 返回至少 1 个（如对乙酰氨基酚）（验证：单测确认 len(result) ≥ 1）

### F6: 组件协作
- [ ] recommend_node 在 Neo4j 可用时走图查询路径（验证：日志中出现 "KG candidate" 相关字样）
- [ ] recommend_node 在 Neo4j 不可用时降级到 PG ILIKE（验证：关闭 Neo4j 后推荐仍能工作）

## 编译与测试
- [ ] 项目编译/导入无错误（验证：`python -c "import app.main"`）
- [ ] AC8: T11 Neural4jClient 单测全 pass（验证：`pytest tests/unit/test_kg/test_client.py -v`）
- [ ] AC8: T12 DrugGraphRepository 单测全 pass（验证：`pytest tests/unit/test_kg/test_repository.py -v`）
- [ ] 所有已有测试仍然通过（验证：`pytest tests/ -v` 无新增失败）
- [ ] AC6: `python data/seed.py` 完整执行无错误（验证：exit code 0，Neo4j 中有数据）

## 端到端场景
- [ ] 场景 1：用户描述"头痛发烧 3 天" → 系统通过 Neo4j 图查询召回布洛芬、对乙酰氨基酚等 → Safety Rules 过滤 → Scorer 排序 → LLM 生成推荐文案 → 返回 Top-3（验证：`pytest tests/acceptance.py -k "E2E"` 通过）
- [ ] 场景 2：Neo4j 不可用 → 系统自动降级到 PG ILIKE → 对话流程不受影响，用户无感知（验证：手动停 Neo4j 后发消息，仍正常推荐）
- [ ] 场景 3：用户说"有胃溃疡，不能用布洛芬" → safety_check_node 从图查询布洛芬禁忌 → Safety Rules Engine 排除布洛芬 → 推荐对乙酰氨基酚（验证：模拟含有 chronic_conditions=["胃溃疡"] 的 consult_slots）
