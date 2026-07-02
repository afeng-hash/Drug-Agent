"""
Rule Engine — 确定性安全规则引擎。

OOP 设计：规则是独立的 SafetyRule 子类，引擎负责编排执行。
执行分两个阶段：
  1. BLOCK 阶段 — 任一规则触发即短路线（如高热、紧急症状 → 立即就医警告）
  2. FILTER 阶段 — 聚合所有排除列表（如过敏史 → 排除含该成分的药品）

与 LLM 的关系：
  规则引擎完全确定性（无 LLM 调用），在 SafetyCheck 节点中运行。
  LLM 负责前面的症状收集和后面的药品推荐，规则引擎在中间做安全把关。
"""

from app.rules.base import RuleResult, SafetyRule


class SafetyResult:
    """规则引擎的聚合输出结果。"""

    def __init__(self):
        self.verdict: str = "PASS"
        """最终结论：
          - "PASS"   → 全部通过，没有规则触发
          - "BLOCK"  → 触发拦截规则，直接终止推荐流程
          - "FILTER" → 排除了部分药品，其余照常推荐"""

        self.triggered_rules: list[dict] = []
        """触发了哪些规则。每项：{rule_id, action, reason} """

        self.excluded_drugs: list[str] = []
        """被排除的药品通用名列表（FILTER 阶段填充）"""

        self.message: str = ""
        """BLOCK 时的警告文案（给用户看的）"""


class RuleEngine:
    """确定性安全规则引擎。

    使用方式：
        engine = RuleEngine()
        engine.register(HighFeverRule())       # 注册规则
        engine.register(InfantFeverRule())
        ...
        result = engine.check(slots, drug_names)  # 执行检查
    """

    def __init__(self):
        self._rules: list[SafetyRule] = []

    def register(self, rule: SafetyRule) -> None:
        """注册一条安全规则。

        在 app 启动时调用，把所有规则注册到引擎中。
        执行时会按注册顺序依次评估。

        Args:
            rule: SafetyRule 的子类实例
        """
        self._rules.append(rule)

    def check(
        self, slots: dict, drug_names: list[str] | None = None
    ) -> SafetyResult:
        """按两阶段执行所有已注册的规则。

        阶段 1 — BLOCK：按注册顺序依次评估，第一个触发的 BLOCK 规则
                  立即短路线返回，不再执行后续规则。
        阶段 2 — FILTER：所有规则评估完后，聚合排除列表（在 candidates
                  中匹配的药品被标记为 excluded）。

        Args:
            slots:      Consult 槽位 dict（症状、体温、年龄、过敏史等）
            drug_names: 候选药品通用名列表（用于 FILTER 匹配）

        Returns:
            SafetyResult 聚合结果
        """
        result = SafetyResult()
        drug_names = drug_names or []

        # ── 阶段 1：BLOCK 规则（短路线） ──────────────────────
        for rule in self._rules:
            rule_result = rule.evaluate(slots)
            if rule_result.triggered and rule_result.action == "BLOCK":
                result.verdict = "BLOCK"
                result.triggered_rules.append({
                    "rule_id": rule.rule_id,
                    "action": "BLOCK",
                    "reason": rule_result.reason,
                })
                # 生成用户可见的警告消息
                result.message = self._build_block_message(result.triggered_rules)
                return result  # ← 短路线！不再执行后续规则

        # ── 阶段 2：FILTER 规则（聚合排除） ──────────────────────
        for rule in self._rules:
            rule_result = rule.evaluate(slots)
            if rule_result.triggered and rule_result.action == "FILTER":
                result.triggered_rules.append({
                    "rule_id": rule.rule_id,
                    "action": "FILTER",
                    "reason": rule_result.reason,
                })
                # 只在候选药品中排除（不在候选列表中的忽略）
                for drug in rule_result.excluded_drugs:
                    if drug in drug_names:
                        result.excluded_drugs.append(drug)

        if result.excluded_drugs:
            result.verdict = "FILTER"

        return result

    @staticmethod
    def _build_block_message(triggered: list[dict]) -> str:
        """拼装 BLOCK 时的用户警告文案。

        包含触发的理由列表和就医建议。
        """
        reasons = [r["reason"] for r in triggered]
        return (
            "⚠️ 系统检测到以下危险信号，建议您立即就医，不要自行用药：\n"
            + "\n".join(f"  • {r}" for r in reasons)
            + "\n\n本系统仅为辅助参考，不能替代专业医疗诊断。请尽快前往医院就诊。"
        )
