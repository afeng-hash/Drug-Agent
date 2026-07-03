"""
Evidence：患者慢性病/特殊人群 与 药品禁忌症冲突检测。

根据用户自述的慢性病史（chronic_conditions）和特殊人群标签
（special_population），与已知的药品禁忌列表交叉比对。

合并策略：min — 任何一条禁忌命中就拉低 safety 值（取最差情况）

两种严重级别：
  - 'absolute' → value=0.0  ← 绝对禁忌，该药品完全不可用
  - 'caution'   → value=0.3  ← 慎用，需医师指导

示例：
  用户有"胃溃疡"，候选药品"布洛芬" → value=0.0（绝对禁忌）
  用户有"哮喘"，候选药品"布洛芬"   → value=0.3（慎用）
  用户无慢性病                    → value=1.0（安全）
"""

from app.scorer.evidence.base import BaseEvidence
from app.scorer.schemas import EvidenceResult


# 已知禁忌症映射：(慢性病关键词, 严重级别, 解释文本)
# 'absolute' = 绝对禁忌 → value=0.0（该药品完全排除）
# 'caution'   = 慎用     → value=0.3（需医师指导）
CONTRAINDICATION_PATTERNS = {
    "布洛芬": [
        ("消化道溃疡", "absolute", "活动性消化道溃疡者禁用布洛芬"),
        ("消化道出血", "absolute", "消化道出血者禁用布洛芬"),
        ("胃溃疡", "absolute", "胃溃疡患者禁用布洛芬"),
        ("严重肝肾", "absolute", "严重肝肾功能不全者禁用布洛芬"),
        ("心力衰竭", "absolute", "严重心力衰竭患者禁用布洛芬"),
        ("哮喘", "caution", "布洛芬可能诱发哮喘，需在医师指导下使用"),
    ],
    "对乙酰氨基酚": [
        ("严重肝肾", "absolute", "严重肝肾功能不全者禁用对乙酰氨基酚"),
        ("肝", "caution", "肝功能不全者应在医师指导下使用对乙酰氨基酚"),
    ],
    "复方氨酚烷胺": [
        ("严重肝肾", "absolute", "严重肝肾功能不全者禁用"),
        ("消化道溃疡", "caution", "消化道溃疡患者慎用"),
    ],
    "阿司匹林": [
        ("消化道溃疡", "absolute", "活动性消化道溃疡者禁用阿司匹林"),
        ("消化道出血", "absolute", "消化道出血者禁用阿司匹林"),
        ("哮喘", "absolute", "阿司匹林哮喘患者禁用"),
        ("妊娠", "absolute", "妊娠晚期禁用阿司匹林"),
        ("出血", "caution", "出血倾向者慎用阿司匹林"),
    ],
}


class ContraindicationCheck(BaseEvidence):
    """慢性病 / 特殊人群与药品禁忌症的冲突检测。

    同时处理：
      1. 药品特定的禁忌症（CONTRAINDICATION_PATTERNS）
      2. 通用特殊人群限制（孕妇/哺乳期 — 不区分具体药品）
    """

    @property
    def feature_name(self) -> str:
        return "safety"

    @property
    def description(self) -> str:
        return "用户慢性病/特殊人群与药品禁忌症的冲突检测"

    def evaluate(self, slots: dict, drug) -> EvidenceResult:
        chronic = slots.get("chronic_conditions", [])
        special_pop = slots.get("special_population")

        # ── 汇集所有条件标签 ──
        conditions = list(chronic) if chronic else []
        if special_pop:
            conditions.append(special_pop)

        if not conditions:
            # 无风险因素 → 满分安全
            return EvidenceResult(
                feature_name=self.feature_name,
                value=1.0,
                reason="用户无慢性病史或特殊人群标签，无禁忌冲突",
                merge_strategy="min",
            )

        drug_name = drug.generic_name

        # ── 通用特殊人群检查（不区分药品） ──
        if special_pop in ("孕妇", "pregnant"):
            return EvidenceResult(
                feature_name=self.feature_name,
                value=0.3,
                reason="孕妇属于特殊人群，应在医师指导下用药",
                merge_strategy="min",
            )
        if special_pop in ("哺乳期", "breastfeeding"):
            return EvidenceResult(
                feature_name=self.feature_name,
                value=0.5,
                reason="哺乳期妇女应在医师指导下用药",
                merge_strategy="min",
            )

        # ── 药品特定禁忌症检查 ──
        patterns = CONTRAINDICATION_PATTERNS.get(drug_name, [])

        for condition in conditions:
            for pattern_kw, severity, explanation in patterns:
                if pattern_kw in condition:
                    if severity == "absolute":
                        # 绝对禁忌 → safety=0.0，后续 ScoringEngine 会排除该药品
                        return EvidenceResult(
                            feature_name=self.feature_name,
                            value=0.0,
                            reason=explanation,
                            merge_strategy="min",
                        )
                    else:
                        # 慎用 → safety=0.3，降低推荐优先级
                        return EvidenceResult(
                            feature_name=self.feature_name,
                            value=0.3,
                            reason=explanation,
                            merge_strategy="min",
                        )

        # 无匹配 → 安全
        return EvidenceResult(
            feature_name=self.feature_name,
            value=1.0,
            reason=f"用户情况[{', '.join(conditions)}]与该药品无已知禁忌冲突",
            merge_strategy="min",
        )
