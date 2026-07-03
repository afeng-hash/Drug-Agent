"""
Drug Scorer — 确定性 OTC 药品评分与排序子系统。

这是推荐环节的核心评分引擎。当安全筛查通过后，ScoringPipeline 接管：
  1. 加载权重配置（从 PostgreSQL weights_config 表）
  2. 对每个候选药品执行 7 条证据规则，生成特征向量（FeatureVector）
  3. 用权重 × 特征值 = 加权总分
  4. 按总分降序排列，返回 Top-N 推荐

为什么是"确定性"？
  - 全部纯函数，无 LLM 参与评分（LLM 只负责后续的推荐理由文案生成）
  - 相同输入 → 相同输出，100% 可复现
  - 评分逻辑可审计、可调优

架构层次：
  ┌──────────────────────────────────────────┐
  │            ScoringPipeline               │  ← 编排层（recommend_node 调用的唯一入口）
  ├──────────────────────────────────────────┤
  │  EvidenceEngine  │  ScoringEngine        │  ← 计算层
  │  (slots,drug)→FV  │  (FV,weights)→score  │
  ├──────────────────────────────────────────┤
  │  7 Evidence Rules │  WeightConfigRepo    │  ← 规则/数据层
  │  (症状/安全/年龄..) │  (PostgreSQL)        │
  └──────────────────────────────────────────┘

使用方式（在 recommend_node 中）：
  result = await scoring_pipeline.run(candidates, slots, session_id, weight_repo)
  for drug in result.drugs[:3]:
      print(f"{drug.generic_name}: {drug.total_score:.2f}")
"""

from app.scorer.schemas import DimensionScore, EvidenceResult, ScoredDrug, ScoringResult
from app.scorer.evidence.base import BaseEvidence

__all__ = [
    "BaseEvidence",
    "DimensionScore",
    "EvidenceResult",
    "ScoredDrug",
    "ScoringResult",
]
