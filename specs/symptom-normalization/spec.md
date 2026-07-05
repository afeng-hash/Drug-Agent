# 症状标准化模块 Spec

## 背景

当前"症状 → KG 查询"链路存在缺口：

- Consult Agent (LLM) 输出的症状名是自然语言（如"喉咙不舒服"）
- KG (Neo4j) 的 Symptom 节点只能精确匹配 `name` 或 `aliases`
- `symptoms.yaml` 手工维护的 aliases 无法穷举所有口语化表达
- 症状匹配失败 → KG 查不到相关药品 → 推荐链路断裂

核心矛盾：**LLM 自由文本 vs KG 受控词汇**。

系统本质是 Symptom → Drug Recommendation System，症状标准化错误会导致 KG 查询错误 → 药品召回错误 → 推荐错误，错误链式放大。因此 **Precision-first**：错映射比漏映射危险得多。

## 目标

1. 在 Consult 和 KG 查询之间插入标准化层，将自由文本症状名映射到 KG 标准名
2. **Precision-first**：错映射比漏映射危险得多，宁可不匹配也不乱映射
3. **风险分层**：粗粒度症状（Level 1）容错空间大、可接受 LLM 映射；细粒度症状（Level 3）不走 LLM，未匹配直接丢弃
4. **性能**：大部分请求走确定性匹配（<1ms），LLM 仅兜底（低频触发）
5. **可维护**：词表从 Neo4j 读取，后台管理系统增删症状后自动感知
6. **可观测**：每次映射记录匹配方式（exact/alias/contains/llm），便于审计和调优

## 功能需求

### F1: 词表加载（Vocabulary Source）

从 Neo4j 加载全部 Symptom 节点（name, level, aliases），启动时加载到内存，运行时零 Neo4j 开销。通过接口抽象，未来可切换数据源。

### F2: Layer 0 — 确定性匹配

无需 LLM，纯内存查字典，按优先级降级：

| 优先级 | 策略 | 说明 |
|--------|------|------|
| 1 | Exact | raw == symptom.name |
| 2 | Alias | raw 在 symptom.aliases 中 |
| 3 | Contains | 双向包含匹配，取最长命中（如"一直干咳"→"干咳"） |

### F3: Layer 1 — LLM + 硬词表约束

仅对 Layer 0 未匹配的症状触发。关键约束：
- LLM 输出空间限制在 Neo4j 词表内，返回不在词表中的名称 → 丢弃
- Prompt 包含 IS_A 层级关系，帮助 LLM 做医学概念判断
- Temperature = 0，保证确定性
- 允许返回 null（不确定就不映射）
- 结果缓存（进程内），同 session 不重复调用

### F4: 风险分层策略

不同层级症状采用不同的接受阈值：

| 症状层级 | 示例 | Layer 0 未匹配时 |
|---------|------|-----------------|
| Level 1 (粗粒度) | 头痛、发热、咳嗽 | 接受 LLM 映射，中等置信度即可 |
| Level 2 (中等粒度) | 偏头痛、干咳、湿咳 | LLM 映射需高置信度 |
| Level 3 (细粒度) | 太阳穴跳痛、晨起咽干 | **不走 LLM**，直接丢弃 |

### F5: 集成点

标准化发生在 `recommend_node` 中，`symptom_weights` 构建之后、KG 查询之前。对上游 Consult 和下游 KG Repository 均透明。

### F6: 可观测性

每次归一化记录：
- `method`：匹配方式（exact / alias / contains / llm）
- `confidence`：置信度
- `raw → standard`：映射前后的名称

通过 `node_events` 透出，可审计每次推荐的质量。

## 非功能需求

### N1: 性能

| 指标 | 目标 |
|------|------|
| Layer 0 延迟 | <5ms（纯内存，无 IO） |
| Layer 1 延迟（LLM 兜底触发时） | <500ms |
| 词表加载（启动时一次） | <200ms |
| 80%+ 的请求落在 Layer 0，无额外 LLM 开销 | — |

### N2: 正确性

- **错映射率 = 0**：Layer 1 返回名称不在 Neo4j 词表 → 丢弃；Level 3 症状不走 LLM
- 映射结果可复现（Temperature=0，确定性匹配规则固定）

### N3: 健壮性

- Neo4j 不可用或 LLM 不可用时：**不做降级回退，仅打印错误日志**
- 任何异常不阻塞主推荐链路

### N4: 可维护性

- `VocabularySource` 接口抽象，当前实现从 Neo4j 读取
- 症状增删改（通过管理后台 → Neo4j）后，下次启动或手动刷新即生效
- 每步匹配记录 `method` 字段，日志可审计

## 不做的事

- ❌ 不去修改 Consult Agent 的 prompt（Consult 继续自由输出自然语言）
- ❌ 不做 Embedding 相似度匹配（语义相似 ≠ 医学等价）
- ❌ 不做一对多映射（如"感冒症状"→多个症状，由 Consult Agent 拆分，不是归一化层的职责）
- ❌ 不做用户反馈闭环（如根据用户选择修正映射——这属于后续迭代）
- ❌ 不修改 KG Repository 的 Cypher 查询逻辑
- ❌ 不维护 `symptoms.yaml` aliases 的持续更新（后续由管理后台 + Neo4j 取代）

## 验收标准

- **AC1**: 标准名精确匹配——输入"干咳"，输出标准名"干咳"，method=exact，confidence=1.0
- **AC2**: 别名匹配——输入"嗓子疼"，输出标准名"咽喉痛"，method=alias，confidence=1.0
- **AC3**: 包含匹配——输入"一直咳嗽"，输出标准名"咳嗽"，method=contains
- **AC4**: LLM 有效映射——输入"喉咙不舒服"（不在 alias 中），LLM 返回"咽喉痛"（在词表中），接受
- **AC5**: LLM 幻觉阻断——LLM 返回一个不在词表中的名称 → 丢弃，保留原始名
- **AC6**: Level 3 保护——Level 3 症状（如"晨起咽干"）Layer 0 未匹配 → 不走 LLM，直接丢弃
- **AC7**: 词表从 Neo4j 加载——启动后 normalizer 可用，词表非空
- **AC8**: 性能——Layer 0 处理 10 个症状 <10ms
- **AC9**: 可观测——每次归一化结果记录 method 和 confidence
