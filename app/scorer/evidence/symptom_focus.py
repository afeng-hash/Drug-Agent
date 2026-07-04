"""
Evidence：症状聚焦率 — 药物的专注度（匹配症状数 / 药物总治疗症状数）。

与 symptom_match（coverage×specificity，图谱加权连续分）互补：
  - symptom_match：  "这药能不能治？治得好不好？"（IS_A 层级 + 强度衰减 + 精确度惩罚）
  - focus_ratio：    "这药是不是专门治这个的？"（纯集合比例，专药高分/广谱药低分）

合并策略：set — 这是从 KG 数据直接计算的确定性属性，不存在多条证据竞争。

几何平均下的行为：
  - focus_ratio=1.0 → exp(w×ln(1.0))=1.0 → 完全中性（极专药，全部治疗症状都命中）
  - focus_ratio=0.1 → exp(w×ln(0.1)) < 1.0 → 显著惩罚（广谱药，仅少量症状匹配）

为什么是 matched/drug_total_treats 而不是 matched/user_symptoms：
  - matched/user_symptoms 在单症状场景下永远=1.0，零区分度
  - matched/drug_total_treats 惩罚广谱药、奖励专药，单症状也能拉开差距
  - 用户视角的覆盖度已由 symptom_match 的 coverage 分体现，无需重复

数据来源：
  drug._graph_matched_count  ← KG Cypher 返回的 matched_symptom_count
  drug._graph_total_treats   ← KG Cypher 返回的 drug_total_treats
"""

from app.scorer.evidence.base import BaseEvidence
from app.scorer.schemas import EvidenceResult


class SymptomFocusRatio(BaseEvidence):
    """药品症状聚焦率 = |matched| / |drug_total_treats|。

    衡量"这个药是专门治用户症状的，还是一个什么都能治的广谱药"。
    专药（镇咳药只治2-3种症状，匹配1个）→ 高分；
    广谱药（感冒药治7+种症状，匹配1个）→ 低分。

    与 symptom_match 互补：
      - symptom_match 回答"能不能治"（图谱覆盖度 × 精确度）
      - focus_ratio   回答"是不是专治这个"（药物专注度）
    """

    @property
    def feature_name(self) -> str:
        return "symptom_focus_ratio"

    @property
    def description(self) -> str:
        return "药品症状聚焦率（|匹配症状|/|药品总治疗症状|），专药高分/广谱药低分"

    def evaluate(self, slots: dict, drug) -> EvidenceResult:
        """计算症状聚焦率 = 匹配症状数 / 药物总治疗症状数。

        数据来源：
          - 分子：drug._graph_matched_count（KG 查询返回的独立匹配症状数）
          - 分母：drug._graph_total_treats（KG 查询返回的该药品 TREATS 症状总数）

        当 KG 数据不可用时，返回 1.0（中性，不影响几何平均）。
        """
        # ── 从 KG 数据获取匹配症状数和药物总治疗症状数 ──
        matched = getattr(drug, "_graph_matched_count", None)
        total_treats = getattr(drug, "_graph_total_treats", None)

        if matched is None or total_treats is None or total_treats == 0:
            # KG 数据不可用 → 中性（不惩罚）
            return EvidenceResult(
                feature_name=self.feature_name,
                value=1.0,
                reason="KG数据不可用，聚焦率默认中性",
                merge_strategy="set",
            )

        if matched == 0:
            # 没有任何症状匹配 → 最低分（几何平均中会被严重惩罚）
            return EvidenceResult(
                feature_name=self.feature_name,
                value=0.0,
                reason="药品未匹配任何用户症状",
                merge_strategy="set",
            )

        ratio = min(matched / total_treats, 1.0)

        return EvidenceResult(
            feature_name=self.feature_name,
            value=ratio,
            reason=(
                f"药品专治{total_treats}种症状，命中用户{matched}种 → 聚焦率={ratio:.2f}"
                if matched < total_treats
                else f"药品专治{total_treats}种症状，全部命中用户症状 → 极高聚焦"
            ),
            merge_strategy="set",
        )
