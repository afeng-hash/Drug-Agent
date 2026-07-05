"""症状标准化模块 — 将用户自由文本症状名映射到 KG 标准症状词表。

两级匹配策略：
  Layer 0 (确定性, <1ms): exact → alias → contains
  Layer 1 (LLM + 硬词表约束):  仅对 Layer 0 未匹配且风险可接受的症状调用
  风险分层: Level 3 细粒度症状不走 LLM

使用方式：
    from app.normalizer import SymptomNormalizer
    from app.normalizer.vocabulary import Neo4jVocabularySource

    vocab = Neo4jVocabularySource(neo4j_client)
    await vocab.load()
    normalizer = SymptomNormalizer(vocab=vocab, llm_client=llm_client)
    result = await normalizer.normalize(["喉咙不舒服", "干咳"])
"""

from app.normalizer.symptom_normalizer import SymptomNormalizer
from app.normalizer.schemas import NormalizedSymptom, NormalizationResult

__all__ = ["SymptomNormalizer", "NormalizedSymptom", "NormalizationResult"]
