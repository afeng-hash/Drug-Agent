"""
Rule engine base types — 安全规则的抽象定义。

所有安全规则都继承 SafetyRule 基类并实现 evaluate() 方法。
每个规则返回 RuleResult（是否触发、触发后采取什么行动、理由）。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class RuleResult:
    """单条规则的评估结果。"""

    triggered: bool = False
    """规则是否被触发（条件满足）"""

    action: str = "NONE"
    """触发后的行动：
      - "NONE"   ← 未触发，不做任何事
      - "BLOCK"  ← 拦截，阻止整个推荐流程，返回就医警告
      - "FILTER" ← 过滤，从候选药品中移除某些药品"""

    reason: str = ""
    """触发理由的人类可读描述，如 "体温 39.5°C 属于高热范围，建议立即就医" """

    excluded_drugs: list[str] = field(default_factory=list)
    """当 action="FILTER" 时，要排除的药品通用名列表"""


class SafetyRule(ABC):
    """安全规则的抽象基类。

    所有具体规则都需实现 evaluate() 方法。

    示例用法（定义一个新规则）：
        class HighFeverRule(SafetyRule):
            rule_id = "r1_high_fever"
            description = "体温超过 39°C 建议立即就医"

            def evaluate(self, slots):
                temp = slots.get("temperature")
                if temp is not None and temp > 39:
                    return RuleResult(
                        triggered=True,
                        action="BLOCK",
                        reason=f"体温 {temp}°C 属于高热..."
                    )
                return RuleResult()  # 未触发
    """

    rule_id: str = ""
    """规则唯一标识，如 "r1_high_fever"。用于日志和调试"""

    description: str = ""
    """规则描述，方便维护者理解规则意图"""

    @abstractmethod
    def evaluate(self, slots: dict) -> RuleResult:
        """评估此规则。

        由 RuleEngine 调用。
        传入 consult_slots dict，返回评估结果。

        Args:
            slots: 症状槽位 dict，键值可能为：
              - symptoms (list[dict])         ← 症状列表
              - temperature (float|None)      ← 体温℃
              - duration_days (int|None)      ← 持续天数
              - medications_taken (list[str]) ← 已服药物
              - special_population (str|None) ← 特殊人群
              - age (int|None)                ← 年龄
              - chronic_conditions (list[str])← 慢性病史
              - allergies (list[str])         ← 过敏史
              - other_symptoms (list[str])    ← 伴随症状

        Returns:
            RuleResult：是否触发、采取什么行动、理由、排除哪些药品
        """
        ...
