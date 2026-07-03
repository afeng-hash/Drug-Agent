"""
ScoringEngine — 纯函数：FeatureVector × Weights → ScoredDrug 评分。

这是评分管线的第三步（第四步是排序后生成推荐文案）。
完全确定性：无 IO、无随机数、无全局状态。相同输入 100% 可复现。

核心公式：
  total_score = Σ (w_i × f_i)  /  Σ w_i

  其中：
    w_i = 权重配置中第 i 个维度的权重（来自 PostgreSQL weights_config 表）
    f_i = 证据引擎输出的第 i 个特征值（FeatureVector，来自 EvidenceEngine）

  权重会在内部自动归一化（使 Σw_i = 1.0），因此总分范围 0.0 ~ 1.0。

安全阈值（safety_threshold）：
  如果 features['safety'] < safety_threshold，该药品被标记为 excluded=True。
  被排除的药品不计入推荐列表。默认阈值 0.2。
"""

import time

from app.scorer.schemas import DimensionScore, ScoredDrug, ScoringResult


# ═══════════════════════════════════════════════════════════════
# 内部工具函数
# ═══════════════════════════════════════════════════════════════

def _normalize_weights(weights: dict[str, float]) -> dict[str, float]:
    """归一化权重使总和为 1.0。

    数据库中的权重是"相对重要程度"（如 symptom_match=30, safety=25），
    需要归一化后才能保证总分在 0~1 范围内。

    Args:
        weights: 原始权重 dict，如 {"symptom_match": 30, "safety": 25, ...}

    Returns:
        归一化后的权重 dict，如 {"symptom_match": 0.30, "safety": 0.25, ...}
        如果所有权重都是 0，则原样返回（避免除以零）
    """
    total = sum(weights.values())
    if total == 0:
        return weights
    return {k: v / total for k, v in weights.items()}


def _collect_evidence_reasons(
    feature_name: str, evidence_results: list
) -> list[str]:
    """从详细证据结果中提取特定 feature_name 的所有理由文本。

    用于生成 DimensionScore 的解释链，让审计和调试有迹可循。

    Args:
        feature_name: 目标特征维度
        evidence_results: EvidenceResult 列表

    Returns:
        该特征维度下所有证据规则的理由文本列表
    """
    if not evidence_results:
        return []
    reasons = []
    for r in evidence_results:
        if r.feature_name == feature_name and r.reason:
            reasons.append(r.reason)
    return reasons


# ═══════════════════════════════════════════════════════════════
# 核心评分函数
# ═══════════════════════════════════════════════════════════════

def score_one(
    features: dict[str, float],
    weights: dict[str, float],
    drug_id: int,
    generic_name: str,
    safety_threshold: float = 0.2,
    evidence_details: list | None = None,
) -> ScoredDrug:
    """对单个药品执行评分。

    公式：total_score = Σ (norm_weight_i × feature_i)
    安全筛查：如果 safety < threshold → excluded=True

    Args:
        features:          EvidenceEngine 输出的特征向量，
                          如 {"symptom_match": 0.85, "safety": 1.0, ...}
        weights:           原始权重配置（内部会归一化），
                          如 {"symptom_match": 30, "safety": 25, ...}
        drug_id:           药品 ID
        generic_name:      药品通用名
        safety_threshold:  safety 特征的排除阈值。safety < 此值则排除该药品
        evidence_details:  可选的 EvidenceResult 列表（用于生成维度解释）

    Returns:
        ScoredDrug：含总分、各维度明细、排除标记
    """
    # ── 归一化权重 ──
    norm_weights = _normalize_weights(weights)

    # ── 安全筛查 ──
    # safety 是多个安全相关证据（禁忌症、过敏、特殊人群）经 min 合并后的综合值
    safety_value = features.get("safety", 1.0)
    if safety_value < safety_threshold:
        reasons = _collect_evidence_reasons("safety", evidence_details or [])
        return ScoredDrug(
            drug_id=drug_id,
            generic_name=generic_name,
            total_score=0.0,
            excluded=True,
            exclude_reason=(
                f"safety({safety_value:.2f}) < threshold({safety_threshold}): "
                f"{'; '.join(reasons[:2])}"  # 只取前两条理由（避免过长）
            ),
        )

    # ── 计算各维度贡献 ──
    dimensions: list[DimensionScore] = []
    total = 0.0

    for feature_name, weight in norm_weights.items():
        fv = features.get(feature_name, 0.0)   # 特征值（如果 Engine 没输出该维度，默认 0）
        contribution = weight * fv              # w × f
        total += contribution
        reasons = _collect_evidence_reasons(feature_name, evidence_details or [])
        dimensions.append(DimensionScore(
            feature_name=feature_name,
            weight=weight,
            feature_value=fv,
            contribution=contribution,
            evidence_reasons=reasons,
        ))

    return ScoredDrug(
        drug_id=drug_id,
        generic_name=generic_name,
        total_score=round(total, 4),   # 四舍五入到 4 位小数，避免浮点噪音
        dimensions=dimensions,
        excluded=False,
    )


def score_all(
    drugs: list,
    features_list: list[dict[str, float]],
    weights: dict[str, float],
    safety_threshold: float = 0.2,
    evidence_details_list: list[list] | None = None,
) -> ScoringResult:
    """批量评分并排序所有候选药品。

    对每个候选药品调用 score_one()，然后排序：
      - 非排除药品在前，按 total_score 降序
      - 排除药品在末尾

    Args:
        drugs:                 Drug ORM 实例列表
        features_list:         每个药品的 FeatureVector，与 drugs 一一对应
        weights:               原始权重配置
        safety_threshold:      safety 排除阈值
        evidence_details_list: 每个药品的 EvidenceResult 列表（可选）

    Returns:
        ScoringResult：已排序的药品列表 + 性能耗时
    """
    t0 = time.perf_counter()

    scored: list[ScoredDrug] = []
    for i, drug in enumerate(drugs):
        features = features_list[i]
        details = evidence_details_list[i] if evidence_details_list else None
        sd = score_one(
            features=features,
            weights=weights,
            drug_id=drug.id,
            generic_name=drug.generic_name,
            safety_threshold=safety_threshold,
            evidence_details=details,
        )
        scored.append(sd)

    # 排序：非排除的在前（按分数降序），排除的在后
    scored.sort(key=lambda d: (d.excluded, -d.total_score))

    elapsed = (time.perf_counter() - t0) * 1000  # 转换为毫秒

    return ScoringResult(
        drugs=scored,
        total_time_ms=round(elapsed, 3),
    )
