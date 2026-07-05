# 症状标准化模块 Plan

## 架构概览

三个核心组件：

| 组件 | 职责 | 依赖 |
|------|------|------|
| `VocabularySource` (接口 + Neo4j 实现) | 从 Neo4j 加载 Symptom 节点到内存，提供查询 | Neo4jClient |
| `SymptomNormalizer` | 编排 Layer 0/1，接收原始名列表，返回标准化结果 | VocabularySource, LLMClient |
| 集成点（`recommend_node` 内） | 构造 normalizer，调用 normalize()，替换 symptom_weights 中的 name | SymptomNormalizer |

```
┌─ Consult Node ──────────────────────────────────────────┐
│ 输出: slots.symptoms = [{name: "喉咙不舒服"}, {name: "干咳"}]  │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
┌─ Recommend Node ────────────────────────────────────────┐
│  symptom_weights 构建后                                  │
│       │                                                │
│       ▼                                                │
│  ┌──────────────────────────────────────────┐         │
│  │        SymptomNormalizer                  │         │
│  │                                          │         │
│  │  ┌────────────────────────────────┐     │         │
│  │  │ VocabularySource (接口)         │     │         │
│  │  │  └─ Neo4jVocabularySource      │     │         │
│  │  │     启动时加载词表到内存         │     │         │
│  │  └────────────────────────────────┘     │         │
│  │                                          │         │
│  │  ┌────────────────────────────────┐     │         │
│  │  │ Layer 0: 确定性匹配              │     │         │
│  │  │  exact → alias → contains       │     │         │
│  │  └────────────┬───────────────────┘     │         │
│  │               │ 未匹配的                  │         │
│  │               ▼                          │         │
│  │  ┌────────────────────────────────┐     │         │
│  │  │ Layer 1: LLM + 硬词表约束        │     │         │
│  │  │  + 风险分层 (L1/L2/L3)          │     │         │
│  │  │  + 结果缓存                      │     │         │
│  │  └────────────────────────────────┘     │         │
│  └──────────────────────────────────────────┘         │
│       │                                                │
│       ▼                                                │
│  symptom_weights (name 已替换为标准名)                    │
│       │                                                │
│       ▼                                                │
│  KG 查询 → 评分 → 推荐                                   │
└────────────────────────────────────────────────────────┘
```

---

## 核心数据结构

### SymptomEntry

```python
class SymptomEntry:
    name: str            # 标准名，如 "咽喉痛"
    level: int           # 1(coarse) / 2(specific) / 3(fine-grained)
    aliases: list[str]   # 别名列表，如 ["嗓子疼", "喉咙痛", ...]
    parents: list[str]   # IS_A 父节点名称列表
```

### VocabularySource（接口）

```python
class VocabularySource(ABC):
    @abstractmethod
    async def load() -> list[SymptomEntry]: ...
    @abstractmethod
    def get_by_name(name: str) -> SymptomEntry | None: ...
    @abstractmethod
    def resolve_alias(alias: str) -> str | None: ...
    @abstractmethod
    def all_names() -> list[str]: ...
    @abstractmethod
    def all_aliases() -> list[str]: ...
```

### NormalizedSymptom

```python
class NormalizedSymptom:
    raw: str              # 原始输入
    standard: str          # 标准化名
    confidence: float      # 0.0 ~ 1.0
    method: str            # "exact" | "alias" | "contains" | "llm"
    level: int             # 匹配到的 KG 症状层级
```

### NormalizationResult

```python
class NormalizationResult:
    results: list[NormalizedSymptom]
    total_time_ms: float
    llm_calls: int
    cache_hits: int
    discarded_count: int   # 因风险分层被丢弃的个数
```

### SymptomNormalizer

```python
class SymptomNormalizer:
    def __init__(self, vocab: VocabularySource, llm_client=None): ...

    async def normalize(self, raw_names: list[str]) -> NormalizationResult: ...

    def _match_layer0(self, raw: str) -> NormalizedSymptom | None:
        """exact → alias → contains。命中返回结果，未命中返回 None。"""

    async def _match_layer1(self, unmatched: list[str]) -> dict[str, str|None]:
        """LLM + 硬词表约束 + 风险分层。"""

    def _risk_accept(self, entry: SymptomEntry, confidence: float) -> bool:
        """Level 1: ≥0.7, Level 2: ≥0.85, Level 3: 不接受"""
```

---

## 模块交互

```
启动时:
  Neo4jVocabularySource.load()
    → MATCH (s:Symptom) RETURN s.name, s.level, s.aliases
    → 构建内存索引 (name→SymptomEntry, alias→name)

运行时（每次 recommend_node 调用）:
  recommend_node
    → SymptomNormalizer.normalize(raw_names)
        ├─ _match_layer0(raw): exact → alias → contains
        │  命中 → NormalizedSymptom
        │  未命中 → unmatched[]
        ├─ 对 unmatched 中 level≠3 的:
        │    _match_layer1(unmatched)
        │      → LLM.generate_structured()
        │      → 验证返回名 ∈ 词表
        │      → _risk_accept(level, confidence)
        │      → 写入缓存
        └─ 返回 NormalizationResult
              → recommend_node 替换 symptom_weights[].name
              → 继续 KG 查询
```

### 风险分层规则

| 症状层级 | LLM 映射后 |
|---------|-----------|
| Level 1 | 置信度 ≥ 0.7 → 接受 |
| Level 2 | 置信度 ≥ 0.85 → 接受 |
| Level 3 | **不走 LLM**（Layer 0 未匹配即丢弃） |

---

## 文件组织

```
app/
  normalizer/
    __init__.py                  # 导出
    schemas.py                   # NormalizedSymptom, NormalizationResult
    vocabulary.py                # VocabularySource(ABC), Neo4jVocabularySource, SymptomEntry
    symptom_normalizer.py        # SymptomNormalizer
  graph/
    nodes/
      recommend.py               # [修改] 集成 normalizer 调用

tests/
  unit/
    test_symptom_normalizer.py   # 全量单元测试
```
