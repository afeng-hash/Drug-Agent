"""
ScoringEngine — 纯函数：FeatureVector × Config → ScoredDrug 评分。

这是评分管线的第三步（第四步是排序后生成推荐文案）。
完全确定性：无 IO、无随机数、无全局状态。相同输入 100% 可复现。

支持两个评分公式版本（由 WeightConfig.scoring_version 控制）：

  v1 — 几何加权平均（向后兼容）:
    total_score = exp( Σ w_i × ln(f_i) ),  Σw_i = 1.0
    四个特征作为平级维度，竞争同一份归一化权重预算。

  v2 — 层级乘法模型（推荐）:
    total_score = symptom_match × focus_ratio^α × age_suitability^β × otc_safety_level^γ
    symptom_match 是主排序信号（指数 1.0，不做任何压缩），
    focus/age/otc 是乘法修正因子（指数 < 1.0 做软惩罚）。

安全阈值（safety_threshold）：
  硬过滤已在评分前由 KG _filter_by_kg_contraindications 完成。
  safety_threshold 保留用于降级场景（KG 不可用时的 PG 证据规则）。
"""

import math
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

def score_one_v1(
    features: dict[str, float],
    weights: dict[str, float],
    drug_id: int,
    generic_name: str,
    safety_threshold: float = 0.2,
    evidence_details: list | None = None,
) -> ScoredDrug:
    """v1 评分公式：几何加权平均。

    公式：total_score = exp( Σ (norm_w_i × ln(f_i)) )
    安全筛查：如果 safety < threshold → excluded=True
    （注意：KG 路径下安全硬过滤已在评分前完成）

    Args:
        features:          EvidenceEngine 输出的特征向量
        weights:           原始权重配置（内部会归一化）
        drug_id:           药品 ID
        generic_name:      药品通用名
        safety_threshold:  safety 特征的排除阈值
        evidence_details:  可选的 EvidenceResult 列表

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

    # ── 几何加权平均 ──
    # score = exp( Σ w_i × ln(f_i) )
    # 其中 w_i 已归一化使 Σw_i = 1.0
    dimensions: list[DimensionScore] = []
    log_total = 0.0

    for feature_name, weight in norm_weights.items():
        # 几何平均下，缺失特征默认为 1.0（中性，不影响分数）
        fv = features.get(feature_name, 1.0)
        # 防止 ln(0)：特征值为 0 时钳制到一个极小的正数
        safe_fv = max(fv, 1e-8)
        log_contribution = weight * math.log(safe_fv)
        log_total += log_contribution
        reasons = _collect_evidence_reasons(feature_name, evidence_details or [])
        # contribution 记录 ln 空间中的贡献（用于调试）
        dimensions.append(DimensionScore(
            feature_name=feature_name,
            weight=weight,
            feature_value=fv,
            contribution=round(log_contribution, 6),
            evidence_reasons=reasons,
        ))

    total_score = round(math.exp(log_total), 4)

    return ScoredDrug(
        drug_id=drug_id,
        generic_name=generic_name,
        total_score=total_score,
        dimensions=dimensions,
        excluded=False,
    )


def score_one_v2(
    features: dict[str, float],
    exponents: dict[str, float],
    drug_id: int,
    generic_name: str,
    evidence_details: list | None = None,
) -> ScoredDrug:
    """v2 评分公式：层级乘法模型。

    公式：
      score = symptom_match × focus_ratio^α × age_suitability^β × otc_safety_level^γ

    其中：
      - symptom_match 是主排序信号（指数 1.0，不做任何压缩）
      - focus_ratio   是纯度折扣（α=0.5 即 sqrt，专药温和惩罚/广谱药显著惩罚）
      - age_suitability 是年龄软惩罚（β=0.3，非成人适度惩罚）
      - otc_safety_level 是 OTC 弱 tiebreaker（γ=0.05，几乎不影响）

    缺失特征使用中性默认值（1.0 = 无惩罚）。

    Args:
        features:   EvidenceEngine 输出的特征向量
        exponents:  惩罚指数 {"focus": 0.5, "age": 0.3, "otc": 0.05}
        drug_id:    药品 ID
        generic_name: 药品通用名
        evidence_details: 可选的 EvidenceResult 列表

    Returns:
        ScoredDrug：含总分、各维度明细、排除标记
    """
    # ── 提取特征值（缺失 → 中性默认 1.0） ──
    sm = max(features.get("symptom_match", 0.5), 1e-8)
    focus = max(features.get("symptom_focus_ratio", 1.0), 1e-8)
    age = max(features.get("age_suitability", 1.0), 1e-8)
    otc = max(features.get("otc_safety_level", 1.0), 1e-8)

    # ── 提取指数（缺失 → 默认值） ──
    alpha = exponents.get("focus", 0.5)
    beta = exponents.get("age", 0.3)
    gamma = exponents.get("otc", 0.05)

    # ── 层级乘法 ──
    focus_factor = focus ** alpha
    age_factor = age ** beta
    otc_factor = otc ** gamma

    total_score = round(sm * focus_factor * age_factor * otc_factor, 4)
    total_score = min(total_score, 1.0)

    # ── 构建维度明细 ──
    reasons_sm = _collect_evidence_reasons("symptom_match", evidence_details or [])
    reasons_focus = _collect_evidence_reasons("symptom_focus_ratio", evidence_details or [])
    reasons_age = _collect_evidence_reasons("age_suitability", evidence_details or [])
    reasons_otc = _collect_evidence_reasons("otc_safety_level", evidence_details or [])

    dimensions = [
        DimensionScore(
            feature_name="symptom_match",
            weight=1.0,
            feature_value=sm,
            contribution=round(sm, 6),
            evidence_reasons=reasons_sm,
        ),
        DimensionScore(
            feature_name="symptom_focus_ratio",
            weight=alpha,
            feature_value=focus,
            contribution=round(focus_factor, 6),
            evidence_reasons=reasons_focus,
        ),
        DimensionScore(
            feature_name="age_suitability",
            weight=beta,
            feature_value=age,
            contribution=round(age_factor, 6),
            evidence_reasons=reasons_age,
        ),
        DimensionScore(
            feature_name="otc_safety_level",
            weight=gamma,
            feature_value=otc,
            contribution=round(otc_factor, 6),
            evidence_reasons=reasons_otc,
        ),
    ]

    return ScoredDrug(
        drug_id=drug_id,
        generic_name=generic_name,
        total_score=total_score,
        dimensions=dimensions,
        excluded=False,
    )


def score_one(
    features: dict[str, float],
    weights: dict[str, float],
    drug_id: int,
    generic_name: str,
    safety_threshold: float = 0.2,
    evidence_details: list | None = None,
    scoring_version: str = "v1",
) -> ScoredDrug:
    """版本分发入口：根据 scoring_version 路由到 v1 或 v2 公式。

    Args:
        features:         EvidenceEngine 输出的特征向量
        weights:          v1=几何权重, v2=惩罚指数
        drug_id:          药品 ID
        generic_name:     药品通用名
        safety_threshold: 安全排除阈值（仅 v1 使用）
        evidence_details: 可选的 EvidenceResult 列表
        scoring_version:  "v1" | "v2"

    Returns:
        ScoredDrug
    """
    if scoring_version == "v2":
        return score_one_v2(
            features=features,
            exponents=weights,  # weights 字段在 v2 中存储的是 exponents
            drug_id=drug_id,
            generic_name=generic_name,
            evidence_details=evidence_details,
        )
    return score_one_v1(
        features=features,
        weights=weights,
        drug_id=drug_id,
        generic_name=generic_name,
        safety_threshold=safety_threshold,
        evidence_details=evidence_details,
    )


def score_all(
    drugs: list,
    features_list: list[dict[str, float]],
    weights: dict[str, float],
    safety_threshold: float = 0.2,
    evidence_details_list: list[list] | None = None,
    scoring_version: str = "v1",
) -> ScoringResult:
    """批量评分并排序所有候选药品。

    对每个候选药品调用 score_one()，然后排序：
      - 非排除药品在前，按 total_score 降序
      - 排除药品在末尾

    Args:
        drugs:                 Drug ORM 实例列表
        features_list:         每个药品的 FeatureVector，与 drugs 一一对应
        weights:               v1=几何权重, v2=惩罚指数
        safety_threshold:      safety 排除阈值（仅 v1 使用）
        evidence_details_list: 每个药品的 EvidenceResult 列表（可选）
        scoring_version:       "v1" | "v2"

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
            scoring_version=scoring_version,
        )
        scored.append(sd)

    # 排序：非排除的在前（按分数降序），排除的在后
    scored.sort(key=lambda d: (d.excluded, -d.total_score))

    elapsed = (time.perf_counter() - t0) * 1000  # 转换为毫秒

    return ScoringResult(
        drugs=scored,
        total_time_ms=round(elapsed, 3),
    )


# ═══════════════════════════════════════════════════════════════
# 对外展示 — Sigmoid 置信度校准
# ═══════════════════════════════════════════════════════════════

# Sigmoid 校准参数（针对 v2 层级乘法模型的分数分布调校）
#   k: 陡峭度——越大过渡越急，越小越平缓
#   midpoint: 置信度 50 分对应的原始分数阈值
#
# 锚点验证（k=12, midpoint=0.18）：
#   v2=0.49 (完美) → 97 分    v2=0.28 (专药) → 77 分
#   v2=0.13 (广谱) → 35 分    v2=0.04 (差)   → 16 分
SIGMOID_K = 12.0
SIGMOID_MIDPOINT = 0.18


def _sigmoid_calibrate(raw_score: float, scale: float = 100.0) -> float:
    """将原始分通过 sigmoid 映射为置信度分数。

    sigmoid(x) = scale / (1 + exp(-k * (x - midpoint)))

    特性：
      - 非线性饱和：高分端和低分端都平缓（不会虚高到 100 或归零）
      - 绝对可比：同一原始分在不同批次映射到相同置信度
      - 中间陡峭：在 midpoint 附近区分度最大
    """
    import math
    return scale / (1.0 + math.exp(-SIGMOID_K * (raw_score - SIGMOID_MIDPOINT)))


def normalize_for_display(
    scored_drugs: list[ScoredDrug],
    scale: float = 100.0,
) -> list[ScoredDrug]:
    """Sigmoid 置信度校准：将原始分映射为绝对置信度分数。

    与排名归一化（min-max / top-relative）的本质区别：
      - 排名归一化回答"这批里谁最好？"——分数随批次波动，最好的永远 100
      - 置信度校准回答"这个推荐有多可信？"——分数是绝对的，诚实反映匹配置信度

    校准后分数的直觉含义：
      90+  → 极高置信度（完美匹配，罕见）
      70-89 → 高置信度（专药精准匹配）
      40-69 → 中等置信度（能用但不是最优）
      20-39 → 低置信度（勉强相关）
      <20   → 极低置信度（基本不相关）

    Args:
        scored_drugs: 评分后的药品列表（已排序）
        scale:        分数上限，默认 100

    Returns:
        同列表，每个 ScoredDrug.display_score 已填充
    """
    active = [d for d in scored_drugs if not d.excluded]

    for d in active:
        d.display_score = round(_sigmoid_calibrate(d.total_score, scale), 1)

    for d in scored_drugs:
        if d.excluded:
            d.display_score = 0.0

    return scored_drugs
