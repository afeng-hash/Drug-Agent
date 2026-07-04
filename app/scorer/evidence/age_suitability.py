"""
Evidence：年龄段适用性 — 检查药品是否有对应年龄段的用法说明。

根据用户的年龄或特殊人群标签判断年龄段，检查药品是否提供了
对应年龄段的用法用量说明。

合并策略：set — 只有这一条规则影响此维度，不存在多条证据竞争

年龄分组与评分：
  儿童 (<12岁 或 special_population="儿童"):
    有 usage_child  → value=0.7（有说明但儿童用药仍需谨慎）
    无 usage_child  → value=0.3（无说明，不推荐）

  老人 (≥60岁 或 special_population="老人"):
    有 usage_elderly → value=0.8（有说明）
    无 usage_elderly → value=0.5（无说明，需谨慎）

  成人（默认）:
    有 usage_adult   → value=1.0（标准情况）
    无 usage_adult   → value=0.5（异常：药品缺少基本说明）
"""

from app.scorer.evidence.base import BaseEvidence
from app.scorer.schemas import EvidenceResult


class AgeSuitability(BaseEvidence):
    """药品对用户年龄段的适用性评估。"""

    @property
    def feature_name(self) -> str:
        return "age_suitability"

    @property
    def description(self) -> str:
        return "药品对用户年龄段的适用性评估"

    def evaluate(self, slots: dict, drug) -> EvidenceResult:
        age = slots.get("age")
        special_pop = slots.get("special_population")

        # ── 判断年龄段 ──
        is_child = False
        is_elderly = False

        if special_pop in ("儿童", "child") or (age is not None and age < 12):
            is_child = True
        elif special_pop in ("老人", "elderly") or (age is not None and age >= 60):
            is_elderly = True

        # ── 儿童 ──
        if is_child:
            if drug.usage_child:
                return EvidenceResult(
                    feature_name=self.feature_name,
                    value=0.7,
                    reason=f"药品有儿童用法用量说明，适用年龄{age}岁",
                    merge_strategy="set",
                )
            else:
                return EvidenceResult(
                    feature_name=self.feature_name,
                    value=0.3,
                    reason=f"药品无儿童专用用法用量说明，不建议{age}岁儿童使用",
                    merge_strategy="set",
                )

        # ── 老人 ──
        if is_elderly:
            if drug.usage_elderly:
                return EvidenceResult(
                    feature_name=self.feature_name,
                    value=0.8,
                    reason=f"药品有老年人用法用量说明，适合{age}岁使用",
                    merge_strategy="set",
                )
            else:
                return EvidenceResult(
                    feature_name=self.feature_name,
                    value=0.5,
                    reason=f"药品无老年人专用说明，{age}岁老年人需谨慎使用",
                    merge_strategy="set",
                )

        # ── 成人（默认） ──
        if drug.usage_adult:
            return EvidenceResult(
                feature_name=self.feature_name,
                value=1.0,
                reason="药品有成人用法用量说明，适合成人使用",
                merge_strategy="set",
            )
        else:
            return EvidenceResult(
                feature_name=self.feature_name,
                value=0.5,
                reason="药品缺少成人用法用量说明",
                merge_strategy="set",
            )
