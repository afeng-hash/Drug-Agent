# 症状标准化模块 Checklist

> 每一项通过运行代码或观察行为来验证。

## 实现完整性
- [ ] [schemas.py] NormalizedSymptom 和 NormalizationResult 可正常导入（验证：`python -c "from app.normalizer.schemas import *"`）
- [ ] [vocabulary.py] Neo4jVocabularySource 可正常导入（验证：`python -c "from app.normalizer.vocabulary import Neo4jVocabularySource"`）
- [ ] [symptom_normalizer.py] SymptomNormalizer 可正常导入（验证：`python -c "from app.normalizer.symptom_normalizer import SymptomNormalizer"`）
- [ ] [__init__.py] 公开符号全部可导入（验证：`python -c "from app.normalizer import SymptomNormalizer, NormalizedSymptom, NormalizationResult"`）
- [ ] [recommend.py] normalizer 集成后原推荐流程不受影响（验证：`pytest tests/ -v` 全部通过）

## Spec 验收标准覆盖
- [ ] AC1 — 精确匹配：输入"干咳" → standard="干咳", method=exact, confidence=1.0
- [ ] AC2 — 别名匹配：输入"嗓子疼" → standard="咽喉痛", method=alias, confidence=1.0
- [ ] AC3 — 包含匹配：输入"一直咳嗽" → standard="咳嗽", method=contains
- [ ] AC4 — LLM 有效映射：输入"喉咙不舒服" → LLM 返回"咽喉痛"（在词表中）→ 接受
- [ ] AC5 — LLM 幻觉阻断：LLM 返回不在词表的名称 → 丢弃，保留原始名
- [ ] AC6 — Level 3 保护：Level 3 症状 Layer 0 未匹配 → 不走 LLM，直接丢弃
- [ ] AC7 — 词表从 Neo4j 加载：启动后 normalizer 可用，词表非空
- [ ] AC8 — 性能：Layer 0 处理 10 个症状 <10ms
- [ ] AC9 — 可观测：每次归一化结果记录 method 和 confidence

## 集成
- [ ] recommend_node 在 symptom_weights 构建后正确调用 normalizer
- [ ] 归一化后的 standard 名正确替换 symptom_weights 中的 name
- [ ] node_events 中记录了归一化统计信息

## 编译与测试
- [ ] 所有新建模块编译无错误
- [ ] 全量单元测试通过（≥ 115 + 新增）
- [ ] 无 import 错误

## 端到端场景
- [ ] 场景 1 — 标准症状直通：用户说"头痛、发热" → Consult 输出"头痛""发热" → normalizer exact 匹配 → KG 正常查询 → 推荐药品
- [ ] 场景 2 — 口语化症状映射：用户说"嗓子疼、一直咳嗽" → normalizer alias("嗓子疼"→"咽喉痛") + contains("一直咳嗽"→"咳嗽") → KG 正常查询
- [ ] 场景 3 — LLM 兜底：用户说"喉咙不舒服"（不在 alias 中）→ Layer 0 未匹配 → LLM 返回"咽喉痛" → 接受 → KG 正常查询
- [ ] 场景 4 — 无法匹配的症状（Level 3）：用户说"晨起咽干" → Layer 0 未匹配 → Level 3 不走 LLM → 丢弃 → 不影响其他症状的推荐
- [ ] 场景 5 — LLM 不可用：normalizer 打印 error 日志，原始名直传 KG
