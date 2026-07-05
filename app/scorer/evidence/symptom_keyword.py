"""
Evidence：症状关键词 → 药品适应症文本匹配。

匹配策略：
  1. 从 consult_slots.symptoms 提取症状名称
  2. 在药品的 indication_summary 中做子串匹配（ILIKE）
  3. 精确匹配得分高于模糊匹配
  4. 无匹配时为 0.0

合并策略：max — 多条 symptom_match 证据取最高值（任一命中就算）

示例：
  slots.symptoms = [{"name": "头痛"}, {"name": "发烧"}]
  drug.indication_summary = "...用于缓解头痛、发热..."
  → value = 1.0（精确匹配，覆盖 ≥50% 症状）
"""

from app.scorer.evidence.base import BaseEvidence
from app.scorer.schemas import EvidenceResult


class SymptomKeywordMatch(BaseEvidence):
    """症状关键词与药品适应症文本的匹配度评估。"""

    @property
    def feature_name(self) -> str:
        return "symptom_match"

    @property
    def description(self) -> str:
        return "症状关键词与药品适应症文本的匹配度"

    def evaluate(self, slots: dict, drug) -> EvidenceResult:
        """评估症状与适应症的文本匹配。

        匹配等级：
          1.0  ← ≥50% 症状精确匹配（如"头痛"完全出现在适应症中）
          0.7  ← 有精确匹配但不足 50%
          0.4  ← 仅有模糊/部分匹配（如单个字匹配）
          0.5  ← 用户未提供症状（中性默认值）
          0.0  ← 完全无匹配
        """
        # ── 提取症状名称 ──
        symptoms = slots.get("symptoms", [])
        symptom_names: list[str] = []
        for s in symptoms:
            if isinstance(s, dict):
                name = s.get("name", "")
            else:
                name = str(s)
            if name:
                symptom_names.append(name)

        if not symptom_names:
            # 中性：用户没说症状，无法评判匹配度
            return EvidenceResult(
                feature_name=self.feature_name,
                value=0.5,
                reason="用户未提供明确症状描述",
                merge_strategy="max",
            )

        # ── 在适应症文本中搜索（所有症状已统一在 symptoms 列表中）──
        indication = (drug.indication_summary or "").lower()

        matched = []
        for kw in symptom_names:
            kw_lower = kw.lower()
            if kw_lower in indication:
                # 精确匹配：整个关键词出现在适应症文本中
                matched.append(kw)
            elif any(ch in indication for ch in kw_lower if ch.strip()):
                # 部分匹配：关键词中的部分字符在适应症中出现
                # （处理中文的部分匹配，如"发热"没有完全匹配但"热"字出现了）
                matched.append(f"{kw}(部分)")

        if matched:
            # 统计精确匹配占比
            exact_matches = [m for m in matched if "(部分)" not in m]
            if len(exact_matches) >= len(symptom_names) * 0.5:
                value = 1.0   # ≥50% 精确匹配 → 高度匹配
            elif exact_matches:
                value = 0.7   # 有精确匹配但不够多 → 中等匹配
            else:
                value = 0.4   # 只有模糊匹配 → 弱匹配
            reason = f"症状[{', '.join(matched)}]在药品适应症中找到匹配"
        else:
            value = 0.0        # 完全没匹配
            reason = f"症状[{', '.join(symptom_names)}]未在药品适应症中找到匹配"

        return EvidenceResult(
            feature_name=self.feature_name,
            value=value,
            reason=reason,
            merge_strategy="max",  # max: 多条 symptom 证据取最高分
        )
