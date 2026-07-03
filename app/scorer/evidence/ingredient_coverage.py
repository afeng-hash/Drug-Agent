"""
Evidence：有效成分覆盖度 和 OTC 安全等级。

两条独立的证据规则：
  1. IngredientCoverage — 药品适应症对用户症状的覆盖比例
  2. OtcSafetyLevel    — 药品 OTC 分类等级（乙类优于甲类）
"""

from app.scorer.evidence.base import BaseEvidence
from app.scorer.schemas import EvidenceResult


# ═══════════════════════════════════════════════════════════════
# 有效成分覆盖度
# ═══════════════════════════════════════════════════════════════

class IngredientCoverage(BaseEvidence):
    """药品适应症对用户症状的覆盖比例。

    计算方式：用户症状列表中，有多少个在药品适应症文本中有匹配。
    覆盖率 = 匹配的症状数 / 总症状数。

    合并策略：max — 覆盖度越高越好，取最高值

    示例：
      症状=["头痛","发烧","流鼻涕"]，适应症覆盖了"头痛""发烧"
      → coverage=2/3≈0.67 → value=0.7（部分覆盖）
    """

    @property
    def feature_name(self) -> str:
        return "ingredient_coverage"

    @property
    def description(self) -> str:
        return "药品适应症对用户症状的覆盖度"

    def evaluate(self, slots: dict, drug) -> EvidenceResult:
        symptoms = slots.get("symptoms", [])
        symptom_names = [
            s.get("name", "") if isinstance(s, dict) else str(s)
            for s in symptoms
        ]

        if not symptom_names:
            # 无已知症状 → 中性
            return EvidenceResult(
                feature_name=self.feature_name,
                value=0.5,
                reason="用户未提供症状，无法计算覆盖度",
                merge_strategy="max",
            )

        indication = (drug.indication_summary or "").lower()
        covered = 0.0
        for name in symptom_names:
            if name.lower() in indication:
                covered += 1.0       # 精确匹配
            elif any(ch in indication for ch in name if ch.strip()):
                covered += 0.5       # 部分匹配

        coverage = covered / len(symptom_names) if symptom_names else 0.0

        # 将覆盖率映射到评分
        if coverage >= 1.0:
            value = 1.0
            reason = f"药品适应症完全覆盖用户症状[{', '.join(symptom_names)}]"
        elif coverage >= 0.5:
            value = 0.7
            reason = f"药品适应症部分覆盖用户症状（{covered}/{len(symptom_names)}）"
        elif coverage > 0:
            value = 0.4
            reason = f"药品适应症少量覆盖用户症状（{covered}/{len(symptom_names)}）"
        else:
            value = 0.1
            reason = f"药品适应症未覆盖用户主要症状[{', '.join(symptom_names)}]"

        return EvidenceResult(
            feature_name=self.feature_name,
            value=value,
            reason=reason,
            merge_strategy="max",
        )


# ═══════════════════════════════════════════════════════════════
# OTC 安全等级
# ═══════════════════════════════════════════════════════════════

class OtcSafetyLevel(BaseEvidence):
    """根据药品 OTC 分类给出安全等级评分。

    中国 OTC 药品分类：
      - 乙类 (Class B)：安全性较高，可在超市销售，无需药师指导 → Score=1.0
      - 甲类 (Class A)：需在药师指导下使用                     → Score=0.7

    合并策略：set — 这是药品的固有属性，不存在多条证据竞争
    """

    @property
    def feature_name(self) -> str:
        return "otc_safety_level"

    @property
    def description(self) -> str:
        return "药品 OTC 分类等级（乙类更安全）"

    def evaluate(self, slots: dict, drug) -> EvidenceResult:
        otc_type = drug.otc_type
        if otc_type == "乙类":
            return EvidenceResult(
                feature_name=self.feature_name,
                value=1.0,
                reason="乙类OTC药品，安全性较高，可在药师指导下购买",
                merge_strategy="set",
            )
        else:
            # 甲类 或 未知 → 默认按甲类处理（偏保守）
            return EvidenceResult(
                feature_name=self.feature_name,
                value=0.7,
                reason=f"甲类OTC药品，需在药师指导下使用",
                merge_strategy="set",
            )
