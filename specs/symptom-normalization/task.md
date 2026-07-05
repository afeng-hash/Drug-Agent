# 症状标准化模块 Tasks

## 文件清单

| 操作 | 文件 | 职责 |
|------|------|------|
| 新建 | `app/normalizer/__init__.py` | 导出所有公开符号 |
| 新建 | `app/normalizer/schemas.py` | NormalizedSymptom, NormalizationResult |
| 新建 | `app/normalizer/vocabulary.py` | VocabularySource(ABC), Neo4jVocabularySource, SymptomEntry |
| 新建 | `app/normalizer/symptom_normalizer.py` | SymptomNormalizer 主逻辑 |
| 修改 | `app/graph/nodes/recommend.py` | 集成 normalizer 调用 |
| 新建 | `tests/unit/test_symptom_normalizer.py` | 全量单元测试 |

---

## T1: 创建数据模型 schemas.py

**文件：** `app/normalizer/schemas.py`
**依赖：** 无

**步骤：**
1. 定义 `NormalizedSymptom` pydantic model，包含字段：raw, standard, confidence, method, level
2. 定义 `NormalizationResult` pydantic model，包含字段：results(list), total_time_ms, llm_calls, cache_hits, discarded_count
3. 添加字段的 Field description 和约束（confidence 0.0~1.0）

**验证：** `python -c "from app.normalizer.schemas import NormalizedSymptom, NormalizationResult; print('OK')"`

---

## T2: 创建词表模块 vocabulary.py

**文件：** `app/normalizer/vocabulary.py`
**依赖：** 无（仅依赖 Neo4jClient 已有的接口）

**步骤：**
1. 定义 `SymptomEntry` dataclass：name, level, aliases, parents
2. 定义抽象接口 `VocabularySource(ABC)`：
   - `async load() -> list[SymptomEntry]`
   - `get_by_name(name) -> SymptomEntry | None`
   - `resolve_alias(alias) -> str | None`
   - `all_names() -> list[str]`
   - `all_aliases() -> list[str]`
3. 实现 `Neo4jVocabularySource(VocabularySource)`：
   - `__init__(self, neo4j_client: Neo4jClient)`
   - `load()`：执行 `MATCH (s:Symptom) RETURN s.name, s.level, s.aliases`，构建 name→entry 和 alias→name 两个内存索引
   - 查询方法从内存索引读取
4. Neo4j 不可用时打印 error 日志

**验证：** 启动 Neo4j（如可用），运行 `python -c "from app.normalizer.vocabulary import Neo4jVocabularySource; print('OK')"`

---

## T3: 创建核心归一化逻辑 symptom_normalizer.py

**文件：** `app/normalizer/symptom_normalizer.py`
**依赖：** T1, T2

**步骤：**
1. 实现 `SymptomNormalizer.__init__(vocab, llm_client)`：
   - 存储 vocab 引用和 llm_client
   - 初始化 `_cache: dict[str, str|None]`
2. 实现 `normalize(raw_names) -> NormalizationResult`（主入口）：
   - 遍历每个 raw_name，调 `_match_layer0`
   - 命中 → 收集结果
   - 未命中 → 加入 unmatched 列表
   - unmatched 中有 level=3 的 → 丢弃（discarded_count++）
   - 剩余 unmatched → 调 `_match_layer1`
   - 组装 NormalizationResult 返回
3. 实现 `_match_layer0(raw) -> NormalizedSymptom | None`：
   - exact match（raw == entry.name）
   - alias match（vocab.resolve_alias）
   - contains match（双向包含，取最长命中）
4. 实现 `_match_layer1(unmatched) -> dict[str, str|None]`：
   - 先查缓存
   - 构建 prompt（词表 + IS_A 层级）
   - 调 `llm_client.generate_structured()`，temperature=0
   - 验证返回名是否在词表内（不在 → 丢弃）
   - 对每个映射调 `_risk_accept` 判断是否接受
   - 结果写入缓存
5. 实现 `_risk_accept(entry, confidence) -> bool`：
   - Level 1 + confidence ≥ 0.7 → True
   - Level 2 + confidence ≥ 0.85 → True
   - 其他 → False
6. 实现 contains 匹配：双向检查 `target in raw or raw in target`，要求 target ≥ 2 字符，多个命中取最长
7. LLM prompt 中构造 schema（Pydantic model），约束输出结构

**验证：** `python -c "from app.normalizer.symptom_normalizer import SymptomNormalizer; print('OK')"`

---

## T4: 创建模块入口 __init__.py

**文件：** `app/normalizer/__init__.py`
**依赖：** T1, T2, T3

**步骤：**
1. 导入并导出 `SymptomNormalizer`, `NormalizedSymptom`, `NormalizationResult`
2. 添加模块 docstring

**验证：** `python -c "from app.normalizer import SymptomNormalizer, NormalizedSymptom, NormalizationResult; print('OK')"`

---

## T5: 集成到 recommend_node

**文件：** `app/graph/nodes/recommend.py`
**依赖：** T4

**步骤：**
1. 导入 `SymptomNormalizer`
2. 在 `symptom_weights` 构建后、`_fetch_candidates` 前，插入归一化调用：
   - 从 symptom_weights 提取 raw_names
   - 创建 `SymptomNormalizer` 实例（传入 vocab 和 llm_client）
   - 调用 `normalize(raw_names)`
   - 用 `result.standard` 替换每个 symptom_weights 的 name
   - 保留 `_raw_name` 字段（供调试）
3. 在 node_events 中记录归一化统计（methods, llm_calls, discarded_count, total_time_ms）

**验证：** 运行现有测试 `pytest tests/ -v`，确保无回归

---

## T6: 编写单元测试

**文件：** `tests/unit/test_symptom_normalizer.py`
**依赖：** T4

**步骤：**
1. 测试 exact match（AC1）
2. 测试 alias match（AC2）
3. 测试 contains match（AC3）
4. 测试 LLM 有效映射（AC4）— mock LLMClient
5. 测试 LLM 幻觉阻断（AC5）— mock 返回不在词表的名称
6. 测试 Level 3 保护（AC6）— Level 3 症状不走 LLM
7. 测试词表加载（AC7）
8. 测试性能（AC8）— Layer 0 10 个症状 < 10ms
9. 测试可观测（AC9）— result 中 method 和 confidence 正确
10. 测试缓存命中
11. 测试空输入、重复症状等边界情况
12. 测试 contains 最长匹配优先
13. 测试风险分层各阈值边界

**验证：** `pytest tests/unit/test_symptom_normalizer.py -v` 全部通过

---

## T7: 全量回归测试

**文件：** 无
**依赖：** T5, T6

**步骤：**
1. 运行 `pytest tests/ -v`
2. 确保 115 个已有测试 + 新增测试全部通过

**验证：** 全量测试通过，零回归

---

## 执行顺序

```
T1 → T2 → T3 → T4 → T5 → T6 → T7
                        ↘ T6（可并行）
```
