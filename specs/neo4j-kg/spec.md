# 知识图谱集成 (Neo4j Knowledge Graph) Spec

## 背景

当前系统的药品查询完全依赖 PostgreSQL ILIKE 模糊匹配：
- `DrugRepository.find_by_symptoms()` 对 `indication_summary` 字段做关键词模糊搜索
- 症状层级关系（如"太阳穴跳痛" → "偏头痛" → "头痛"）无法表达，只能靠 LLM 在 consult 阶段做语义归一化
- 药品间关系（替代药品、相互作用、成分归属）分散在 JSON 字段中，无法做图遍历查询
- 安全过滤（禁忌人群、禁忌病症）目前由 Safety Rules Engine 规则硬编码，新增禁忌需要改代码

引入 Neo4j 的核心价值：**让结构化关系成为一等公民**。图数据库天然适合"症状 A 和 B 什么关系""药 X 禁忌哪些病症""对乙酰氨基酚有哪些替代药"这类多跳关系查询。

## 目标

- 用 Neo4j 存储症状层级、药品-适应症、药品-成分、药品-禁忌、药品间替代/相互作用等结构化关系
- **替换** PostgreSQL ILIKE 模糊匹配，用 Cypher 图遍历做症状→药品精确匹配
- 为 Safety Rules Engine **提供禁忌数据查询接口**（不替代其裁决逻辑）
- 知识图谱作为独立数据服务层，与 PostgreSQL（事务数据）各司其职

## 功能需求

### F1: 图谱数据模型

在 Neo4j 中建立 6 类节点 + 8 类关系：

**节点**：

| 实体 | 关键属性 | 示例 |
|------|---------|------|
| `Symptom` | `name`, `level`(1/2/3) | 头痛(1) → 偏头痛(2) → 太阳穴跳痛(3) |
| `Drug` | `generic_name`, `otc_type`, `dosage_form` | 布洛芬、对乙酰氨基酚 |
| `Ingredient` | `name` | 布洛芬、对乙酰氨基酚、伪麻黄碱 |
| `Category` | `name` | 感冒退烧 |
| `Condition` | `name` | 胃溃疡、哮喘、高血压 |
| `Population` | `name` | 孕妇、哺乳期、儿童、老人 |

**关系**：

| 关系 | 方向 | 关键属性 | 用途 |
|------|------|---------|------|
| `[:TREATS]` | Drug→Symptom | `strength: float` (0-1) | 主推荐路径，区分强适应症/弱覆盖 |
| `[:HAS_INGREDIENT]` | Drug→Ingredient | — | 成分归属 |
| `[:BELONGS_TO]` | Drug→Category | — | 分类 |
| `[:CONTRAINDICATED_FOR]` | Drug→Condition | — | 禁忌病症 |
| `[:CONTRAINDICATED_FOR]` | Drug→Population | — | 禁忌人群 |
| `[:SIMILAR_TO]` | Drug→Drug | — | 替代药品（双向） |
| `[:INTERACTS_WITH]` | Drug→Drug | — | 药物相互作用（双向） |
| `[:IS_A]` | Symptom→Symptom | — | 症状语义层级（子→父，DAG，支持多父节点） |

### F2: 症状→药品图查询

替换 `DrugRepository.find_by_symptoms()` 的 ILIKE 模糊匹配。

**前置条件**：症状文本标准化已由上游节点完成。进入 Neo4j 时 `consult_slots.symptoms` 中每个 symptom 已有标准化的 `canonical_name`（映射后的 Symptom 节点名称）。

**查询流程**：
1. 用标准化症状名精确匹配 Symptom 节点
2. 沿 `[:IS_A]` 向上展开父级症状（transitive，1-2 跳）
3. 匹配到的所有 Symptom 节点 → 沿 `[:TREATS]` 反向找 Drug
4. **排序权重**：
   - 主诉症状（`symptoms[0]`）匹配到的 Drug 权重 × 1.0
   - 附加症状（`other_symptoms`）匹配到的 Drug 权重 × 0.5
   - 再乘以 `[:TREATS].strength`，得到每对 (Drug, Symptom) 的初始得分
5. 同一 Drug 对多个症状的得分求和，降序输出候选列表

### F3: 图谱辅助安全过滤

为 Safety Rules Engine 提供数据查询接口（不做裁决）：

- 药品 + `chronic_conditions` → 查 `Drug-[:CONTRAINDICATED_FOR]->Condition`
- 药品 + `special_population` → 查 `Drug-[:CONTRAINDICATED_FOR]->Population`
- 药品 + `allergies` → 查 `Drug-[:HAS_INGREDIENT]->Ingredient`，判断成分是否匹配过敏原

### F4: 替代药品查询

用户想换药（switch_drug）或首选药被安全过滤排除时：

- 输入药品 → 沿 `[:SIMILAR_TO]` 找到替代药品
- 替代药品重新走 Scorer + 安全过滤

### F5: 图谱数据管理

- **初始化**：seed 脚本从 `data/drugs.json` + 手工整理的 `data/kg/` 目录写入 Neo4j
- **同步**：提供 CLI 命令从 PG `drugs` 表增量同步到 Neo4j
- 图谱数据以 **YAML 文件** 维护（非手写 Cypher），可读可改可版本控制

### F6: 与现有组件的协作边界

```
recommend 节点:
  1. Neo4j 图查询      → 候选药品列表（替代 ILIKE）
  2. Safety Rules Engine → 排除不安全药品（图提供禁忌数据）
  3. Scorer 评分排序    → Top-3（不变）
  4. LLM 生成推荐理由   → 文案（不变）
```

## 非功能需求

### N1: 查询性能
- 症状→药品图查询耗时 < 50ms（P95），单次 Cypher 查询完成
- 不低于现有 PG ILIKE 方案（当前 ≈ 10-30ms）

### N2: 数据一致性
- Neo4j 中的 Drug 节点与 PostgreSQL `drugs` 表保持最终一致
- 同步延迟可接受（分钟级），不需要实时强一致

### N3: 可维护性
- 图谱数据以 YAML 文件维护（6 类节点 + 8 类关系），可读可改可版本控制
- 新增药品关系（如新禁忌症）通过更新 YAML + 重新导入完成，不改代码

### N4: 测试覆盖
- 图查询方法单测覆盖率 ≥ 90%（用 Neo4j 测试容器）
- 降级逻辑有独立测试

## 不做的事

- **不做自动推理**：Neo4j 只存储显式录入的关系。规则推理（如"含布洛芬的药都对胃溃疡禁忌"）属于 Safety Rules Engine 职责
- **不做医学诊断**：系统不走 Symptom→Condition→Drug 路径做诊断式推荐。Condition 节点仅用于禁忌匹配
- **不做症状文本模糊匹配**：症状标准化由上游独立节点完成（将来引入），Neo4j 只做精确图遍历
- **不做实时同步**：PG 药品变更不同步到 Neo4j，同步通过 seed 脚本手动/定时触发
- **不做图可视化**：MVP 阶段不提供 Neo4j Browser 以外的可视化界面
- **不做跨领域扩展**：MVP 只导入现有 12 个感冒退烧药品的数据
- **不替代 RAG**：Neo4j 不存说明书全文，说明书检索仍用 Milvus

## 验收标准

- **AC1**: 输入标准化症状"头痛+发烧"，Neo4j 图查询返回含布洛芬、对乙酰氨基酚的候选列表
- **AC2**: 输入三级症状（经标准化映射为"偏头痛"），通过 `[:IS_A]` 向上匹配到"头痛"，再沿 `[:TREATS]` 找到对应药品
- **AC3**: `[:TREATS].strength` 影响排序：强适应症药排在弱覆盖药前面
- **AC4**: 主诉症状权重 > 附加症状：相同 Drug，主诉命中得分 × 1.0，附加症状命中 × 0.5
- **AC5**: 查询"布洛芬"的禁忌病症，返回"胃溃疡""哮喘"等 Condition 节点
- **AC6**: `seed.py` 跑完后 Neo4j 中包含 12 个 Drug 节点 + 完整关系
- **AC7**: 查询布洛芬的 `[:SIMILAR_TO]` 替代药品，返回至少 1 个（如对乙酰氨基酚）
- **AC8**: 所有图查询方法有独立单测，覆盖率 ≥ 90%
- **AC9**: 一个症状有多个父节点时（DAG），查询正确展开所有父路径，不重复计算 Drug
