# PR Summary：药品推荐评分系统 + Neo4j 知识图谱 + 全代码注释

**Branch**: `master`  
**Base**: `04be30f`（项目骨架 MVP）  
**Commits**: 10 次 | **Files**: 87 变更 | **Diff**: `+9578 / -676`

---

## 一、变更总览

| 类别 | 内容 |
|------|------|
| 🆕 新建 | 44 个文件（评分子系统、知识图谱、规格文档、测试） |
| ✏️ 修改 | 43 个文件（注释、Bug 修复、架构重构） |
| ❌ 删除 | 2 个文件（`r6_drug_allergy.py`、`r7_child_aspirin.py`，已被评分系统取代） |

---

## 二、新增能力

### 2.1 药品评分子系统（`app/scorer/`，~2600 行）

与 LLM 无关的**确定性药品评分管线**——相同输入 100% 可复现：

```
Slot + Drug → EvidenceEngine (8条规则) → FeatureVector → ScoringEngine × Weights → 排序结果
```

| 模块 | 职责 |
|------|------|
| `app/scorer/evidence/` | 8 条证据规则：症状关键词匹配、发热严重度、禁忌症、过敏交叉匹配、年龄段适用性、成分覆盖度、症状聚焦率、知识图谱相关度 |
| `app/scorer/evidence_engine.py` | 规则注册、并行评估、4 种合并策略（`max` / `min` / `avg` / `set`） |
| `app/scorer/engine.py` | **双版本公式**：v1 几何加权平均（向后兼容）、v2 层级乘法 `sm × focus^α × age^β × otc^γ`；Sigmoid 置信度校准（0-100） |
| `app/scorer/pipeline.py` | 一站式编排：加载权重 → 策略校验 → 证据评估 → 评分 → 排序。异常降级为乙类优先排序 |
| `app/scorer/strategy.py` | v1/v2 双版本权重约束校验（`balanced` / `safety_first`），防止运营配出安全权重=0 等不合理配置 |
| `app/scorer/schemas.py` | 完整数据结构：`EvidenceResult` → `FeatureVector` → `DimensionScore` → `ScoredDrug` → `ScoringResult` |

**v2 层级乘法公式**（最新提交 `c6b0795` 引入）：

```python
score = symptom_match × focus_ratio^α × age_suitability^β × otc_safety_level^γ
```

设计哲学：
- `symptom_match` 是主排序信号（指数固定 1.0），不做压缩
- `focus_ratio` 是纯度折扣（α=0.5 即 sqrt）→ 专药温和惩罚 / 广谱药显著惩罚
- `age_suitability` 是年龄软惩罚（β=0.3）→ 非成人适度降分
- `otc_safety_level` 是弱 tiebreaker（γ=0.05）→ 几乎不影响

**Sigmoid 置信度校准**：

```python
display_score = 100 / (1 + e^(-12 × (raw_score - 0.18)))
```

| 原始分 | display_score | 含义 |
|--------|--------------|------|
| 0.49 (完美匹配) | 97 分 | 极高置信度 |
| 0.28 (专药精准) | 77 分 | 高置信度 |
| 0.13 (广谱药品) | 35 分 | 中低置信度 |
| 0.04 (勉强相关) | 16 分 | 低置信度 |

### 2.2 Neo4j 知识图谱子系统（`app/kg/`，~1200 行）

| 模块 | 职责 |
|------|------|
| `app/kg/client.py` | Neo4j 异步驱动封装，连接池、健康检查、**优雅降级**（连不上→回退 PG，不抛异常） |
| `app/kg/repository.py` | Cypher 查询封装：药品-症状-人群-成分关系查询 |
| `app/kg/sync.py` | YAML → Neo4j 数据同步器 |
| `app/kg/schemas.py` | 图谱节点/边数据模型 |
| `data/kg/*.yaml` | 种子数据：86 节点 + 189 关系 |

### 2.3 权重配置 A/B 测试能力

- `WeightConfig` 模型 + `WeightConfigRepository`：多版本权重、按 `session_id` 哈希分桶（100 桶粒度），60s TTL 缓存
- `StrategyValidator`：`balanced` 和 `safety_first` 两种约束策略，v1/v2 独立注册表
- 双评分公式可并行运行（`scoring_version=v1 | v2`）

### 2.4 跨 Turn 状态持久化

- `Session.state_snapshot` JSON 列：每个 turn 结束时写入 `consult_slots`、`phase`、`consult_rounds` 等，解决 Dispatcher 上下文丢失 Bug

### 2.5 测试覆盖（~1500 行）

| 文件 | 覆盖 |
|------|------|
| `tests/unit/test_scoring_pipeline_diag.py` | 评分管线诊断（711 行，含 v2 公式 8 个测试 + Sigmoid 9 个测试） |
| `tests/unit/test_scoring_quality.py` | 评分质量对比（209 行） |
| `tests/unit/test_evidence_rules.py` | 8 条证据规则（180 行） |
| `tests/unit/test_kg/test_client.py` | Neo4j 客户端（112 行） |
| `tests/unit/test_kg/test_repository.py` | 图谱仓库（289 行） |

### 2.6 全代码中文注释

43 个文件添加了详细中文 docstring，覆盖所有类、函数、字段和关键逻辑。

---

## 三、Breaking Changes

### BC-1：`asyncpg` 双重关闭 Bug 修复

```python
# 旧：yield 生成器 + finally close → 与 async_sessionmaker 冲突 → InterfaceError
async def get_db() -> AsyncGenerator:
    async with async_session_factory() as session:
        try:
            yield session
        finally:
            await session.close()

# 新：@asynccontextmanager，close 交给 factory 内部处理
@asynccontextmanager
async def get_db():
    async with async_session_factory() as session:
        yield session
```

**影响**：Bug 修复，不影响 API。所有调用方已改为 `async with get_db() as db:`。

### BC-2：安全检查从 RuleEngine 迁移到 ScoringEngine

删除 `r6_drug_allergy.py` 和 `r7_child_aspirin.py`，逻辑迁移到 `AllergyCheck` 和 `AgeSuitability` 证据规则。

```python
# 旧：RuleEngine 硬删除
rule_engine.filter(slots, candidates) → removed_from_list

# 新：评分系统标记 excluded + 附带原因
score_one(features, weights, safety_threshold=0.2) → ScoredDrug(excluded=True, exclude_reason="...")
```

**影响**：被排除的药品不再静默消失，前端可展示排除原因，提升用户信任度。

### BC-3：函数签名变更（向后兼容）

| 函数 | 变更 |
|------|------|
| `score_one()` | 新增 `scoring_version: str = "v1"` 参数 |
| `score_all()` | 新增 `scoring_version: str = "v1"` 参数 |
| `StrategyValidator.validate()` | 新增 `scoring_version: str = "v1"` 参数 |
| `StrategyValidator.list_strategies()` | 新增 `scoring_version: str = "v1"` 参数 |

默认值保证向后兼容。

### BC-4：`ScoredDrug` 新增 `display_score` 字段

前端需适配：展示分数用 `display_score`（0-100 整数感），`total_score` 仍保留用于排序和调试。

### BC-5：数据库 DDL（部署前必须执行）

```sql
ALTER TABLE sessions ADD COLUMN state_snapshot JSON;
ALTER TABLE weights_config ADD COLUMN scoring_version VARCHAR(10);
```

### BC-6：新增依赖

```diff
+neo4j>=5.26.0
+pyyaml>=6.0
```

Neo4j 为可选依赖，不可用时自动降级到 PG 查询。

---

## 四、业务影响总结

### 对终端用户

| 变化 | 影响 |
|------|------|
| 推荐排序更合理 | 专药得分 ~2× 广谱药，单症状不被泛用药压过；多症状广谱药可反超 |
| 展示分数诚实可信 | Sigmoid 绝对置信度——77 分就是 77 分，不因批次内无好药而虚高 |
| 过敏/禁忌药明确告知 | 被排除药品附带排除原因（"用户对阿司匹林过敏，此药含该成分"） |
| A/B 测试对用户透明 | 按 session_id 哈希分桶，同一用户不会中途切换权重版本 |
| Neo4j 不可用时不受影响 | 自动降级到 PG 查询，推荐功能正常 |

### 对运营人员

| 变化 | 影响 |
|------|------|
| 在线调整推荐策略 | 修改 `weights_config` 表权重即可，无需改代码或重启 |
| 双公式并行对比 | v1.0.0 和 v2.0.0 同时 `is_active=true`，A/B 分桶对比效果 |
| 策略安全约束 | `StrategyValidator` 防止配出危险配置 |

### 对开发人员

| 变化 | 影响 |
|------|------|
| 代码可读性大幅提升 | 43 个文件覆盖中文注释 |
| 确定性评分可调试 | 每个 `DimensionScore` 带 `evidence_reasons`，输入→输出可复现 |
| 测试覆盖到位 | 评分管线、证据规则、Neo4j 均有完整测试 |
| 扩展新评分维度只需 3 步 | 写证据规则 → 注册到 pipeline → 数据库加权重 |

---

## 五、最近提交详情

### `c6b0795` — 评分系统优化：几何平均改用层级乘法，Sigmoid 置信度校准

**Files**: 8 | **Diff**: `+622 / -79`

| 文件 | 变更 |
|------|------|
| `app/scorer/engine.py` | 重构：原 `score_one` → `score_one_v1`，新增 `score_one_v2`（层级乘法），新增版本分发入口，新增 `normalize_for_display()`（Sigmoid 校准） |
| `app/scorer/strategy.py` | v1/v2 双注册表，新增 `BALANCED_V2`、`SAFETY_FIRST_V2` 约束 |
| `app/db/models.py` | 新增 `scoring_version` 字段 |
| `app/scorer/schemas.py` | 新增 `display_score` 字段 |
| `app/scorer/pipeline.py` | 读取 `scoring_version` 并传递给评分引擎和校验器 |
| `app/graph/nodes/recommend.py` | 增加 `normalize_for_display()` 调用，输出改用 `display_score` |
| `data/seed.py` | 插入 v2.0.0 配置，v1.0.0 补充 `scoring_version` 字段，emoji → ASCII |
| `tests/.../test_scoring_pipeline_diag.py` | 新增 v2 公式 8 个测试 + Sigmoid 校准 9 个测试 |
