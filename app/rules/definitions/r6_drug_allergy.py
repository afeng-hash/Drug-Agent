"""
R6: 药物过敏史 → FILTER（排除对应药品）

规则：用户自述有药物过敏史 → 从候选药品中排除含该成分的药品
行动：FILTER — 不影响其他药品推荐

过敏映射表：
  "布洛芬" / "ibuprofen"    → 排除布洛芬
  "阿司匹林" / "aspirin"    → 排除阿司匹林
  "对乙酰氨基酚" / "扑热息痛" → 排除对乙酰氨基酚
  "头孢"                    → 排除头孢氨苄、头孢拉定
"""
from app.rules.base import RuleResult, SafetyRule


# 过敏原 → 要排除的药品通用名
ALLERGY_DRUG_MAP = {
    "布洛芬": ["布洛芬"],
    "ibuprofen": ["布洛芬"],
    "阿司匹林": ["阿司匹林"],
    "aspirin": ["阿司匹林"],
    "对乙酰氨基酚": ["对乙酰氨基酚"],
    "扑热息痛": ["对乙酰氨基酚"],
    "paracetamol": ["对乙酰氨基酚"],
    "头孢": ["头孢氨苄", "头孢拉定"],
    "青霉素": [],     # 暂无 OTC 青霉素类药品，预留
    "磺胺": [],       # 暂无 OTC 磺胺类药品，预留
}


class R6_DrugAllergy(SafetyRule):
    """药物过敏过滤规则。"""

    rule_id = "R6"
    description = "用户自述药物过敏 → 排除对应药物"

    def evaluate(self, slots: dict) -> RuleResult:
        """检查 allergies 槽位，匹配过敏映射表。

        Args:
            slots: consult_slots dict（读取 allergies 列表）

        Returns:
            触发后返回 FILTER 行动 + excluded_drugs 列表
        """
        allergies = slots.get("allergies", [])
        if not allergies:
            return RuleResult()  # 无过敏史 → 不触发

        excluded = []
        reasons = []
        for allergy in allergies:
            allergy_lower = allergy.lower().strip()
            for key, drugs in ALLERGY_DRUG_MAP.items():
                # 模糊匹配：用户说"布洛芬过敏" 或 "ibuprofen" 都能匹配到
                if allergy_lower in key or key in allergy_lower:
                    excluded.extend(drugs)
                    reasons.append(f"用户对{allergy}过敏，排除{'、'.join(drugs)}")

        if excluded:
            return RuleResult(
                triggered=True,
                action="FILTER",
                reason="；".join(reasons),
                excluded_drugs=list(set(excluded)),  # 去重
            )
        return RuleResult()
