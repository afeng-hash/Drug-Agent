"""
Data schemas — Drug Scorer 模块的所有数据结构。

三个层次的数据类：
  1. EvidenceResult  — 单条证据规则的评估输出（属于 Evidence 层）
  2. DimensionScore  — 单个特征维度的得分明细（属于 Scoring 层）
  3. ScoredDrug / ScoringResult — 最终评分结果（Pipeline 输出）

所有类都是 dataclass，无方法逻辑，纯数据容器。
"""

from dataclasses import dataclass, field


# ═══════════════════════════════════════════════════════════════
# Evidence 层 — 证据规则输出
# ═══════════════════════════════════════════════════════════════

@dataclass
class EvidenceResult:
    """单条证据规则的评估结果。

    每条 Evidence 规则回答一个问题：
      "考察了 X 维度，发现 Y 情况，因此将特征 Z 设为值 V"

    示例：
      SymptomKeywordMatch 评估结果：
        feature_name="symptom_match", value=0.7,
        reason="症状[头痛, 发烧]在药品适应症中找到匹配",
        merge_strategy="max"
    """

    feature_name: str
    """这条证据影响的特征维度名，如 'symptom_match' / 'safety' / 'age_suitability'"""

    value: float
    """计算出的特征贡献值，范围 0.0 ~ 1.0。
    0.0 = 完全不匹配/不安全；1.0 = 完美匹配/完全安全"""

    reason: str
    """人类可读的解释文本，用于审计和调试。
    如 "适应症包含'头痛'，与用户症状匹配" """

    merge_strategy: str = "max"
    """当多条证据规则影响同一个特征维度时的合并策略：
      - 'max' ← 取最大值（症状匹配类：任何一条命中就算）
      - 'min' ← 取最小值（安全类：任何一条禁忌就否决）
      - 'avg' ← 取平均值（多条证据等权重贡献）
      - 'set' ← 直接设置（唯一确定的值，如 OTC 分类等级）
    """


# ═══════════════════════════════════════════════════════════════
# Scoring 层 — 评分明细
# ═══════════════════════════════════════════════════════════════

@dataclass
class DimensionScore:
    """单个特征维度对总分的贡献明细。

    记录了权重 w、特征值 f、贡献额 w×f，以及支撑该得分的证据链。

    示例（symptom_match 维度）：
      feature_name="symptom_match", weight=0.30, feature_value=0.85,
      contribution=0.255, evidence_reasons=["症状[头痛]匹配", "含退热成分加成"]
    """

    feature_name: str
    """维度名，如 'symptom_match' / 'safety' / 'age_suitability' """

    weight: float
    """该维度的权重 w（从 weights_config 表加载，已归一化到总和为 1.0）"""

    feature_value: float
    """特征值 f（由 EvidenceEngine 评估得出），范围 0.0 ~ 1.0"""

    contribution: float
    """贡献额 = w × f。所有维度的 contribution 之和 = 药品总分"""

    evidence_reasons: list[str] = field(default_factory=list)
    """支撑该特征值的证据理由列表。来自参与该维度的所有 EvidenceResult.reason"""


@dataclass
class ScoredDrug:
    """单个药品的完整评分结果。

    包含总分、各维度明细、以及排除标记。
    这是 ScoringEngine.score_one() 的输出。
    """

    drug_id: int
    """药品 ID（drugs 表主键）"""

    generic_name: str
    """药品通用名"""

    total_score: float
    """加权总分 Σ(wᵢ × fᵢ)，已归一化到 0.0 ~ 1.0。
    排序时按此字段降序排列"""

    display_score: float = 0.0
    """对外展示分（0-100），由 normalize_for_display() 填充。
    v2 原始分范围较宽，批次内 min-max 归一化后更直观"""

    dimensions: list[DimensionScore] = field(default_factory=list)
    """各维度的得分明细。可用于生成解释文本或可视化"""

    excluded: bool = False
    """是否被安全阈值排除。True 表示该药品因 safety 特征值过低被过滤"""

    exclude_reason: str = ""
    """排除原因（仅 excluded=True 时有值），如 "safety(0.00) < threshold(0.2)" """


@dataclass
class ScoringResult:
    """一次完整评分管线运行的结果。

    包含所有候选药品的评分（已排序），以及该次运行的元数据。
    """

    drugs: list[ScoredDrug] = field(default_factory=list)
    """评分后的药品列表。非排除药品在前，按 total_score 降序；排除药品在末尾"""

    config_version: str = ""
    """本次使用的权重配置版本号，如 'v3.2.1'。用于 A/B 测试追踪"""

    total_time_ms: float = 0.0
    """评分管线总耗时（毫秒），用于性能监控"""
