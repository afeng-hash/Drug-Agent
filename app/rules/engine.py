"""
Rule Engine — 确定性安全规则引擎。

OOP 设计：规则是独立的 SafetyRule 子类，引擎负责编排执行。
只有 BLOCK 阶段 — 任一规则触发即短路线（如高热、紧急症状 → 立即就医警告）。

药品级别的禁忌过滤（FILTER）已移到 recommend_node，
由 Neo4j 知识图谱通过 _filter_by_kg_contraindications() 完成。

与 LLM 的关系：
  规则引擎完全确定性（无 LLM 调用），在 safety_block 节点中运行。
  LLM 负责前面的症状收集和后面的药品推荐，规则引擎在中间做安全把关。
"""

from app.rules.base import RuleResult, SafetyRule


class SafetyResult:
    """规则引擎的聚合输出结果。"""

    def __init__(self):
        self.verdict: str = "PASS"
        """最终结论：
          - "PASS"   → 全部通过，没有规则触发
          - "BLOCK"  → 触发拦截规则，直接终止推荐流程"""

        self.triggered_rules: list[dict] = []
        """触发了哪些规则。每项：{rule_id, action, reason} """

        self.excluded_drugs: list[str] = []
        """[已废弃] 药品级过滤已移到 Neo4j，此字段始终为空。保留以兼容旧代码。"""

        self.message: str = ""
        """BLOCK 时的警告文案（给用户看的）"""


class RuleEngine:
    """确定性安全规则引擎 — 仅 BLOCK 阶段。

    使用方式：
        engine = RuleEngine()
        engine.register(HighFeverRule())       # 注册规则
        engine.register(InfantFeverRule())
        ...
        result = engine.check(slots)  # 执行检查
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

    def check(self, slots: dict) -> SafetyResult:
        """按注册顺序评估所有规则，第一个触发的 BLOCK 规则立即短路线返回。

        Args:
            slots: Consult 槽位 dict（症状、体温、年龄、过敏史等）

        Returns:
            SafetyResult 聚合结果
        """
        result = SafetyResult()

        for rule in self._rules:
            rule_result = rule.evaluate(slots)
            if rule_result.triggered and rule_result.action == "BLOCK":
                result.verdict = "BLOCK"
                result.triggered_rules.append({
                    "rule_id": rule.rule_id,
                    "action": "BLOCK",
                    "reason": rule_result.reason,
                })
                result.message = self._build_block_message(result.triggered_rules)
                return result  # ← 短路线！不再执行后续规则

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
