"""
StrategyValidator — 校验权重配置是否符合预设策略的约束。

为什么需要策略约束？
  权重配置是从数据库动态加载的，可以被运营人员修改。如果没有约束，
  可能会配出"安全权重=0"或"症状匹配权重=0.9"等不合理配置。

  StrategyValidator 在每次评分前校验权重是否落在预设的合理范围内。
  校验失败只记录警告日志，不阻塞推荐流程（防止配置错误导致服务不可用）。

两种内置策略：
  1. balanced      — 症状和安全同等重要（默认策略）
  2. safety_first  — 安全优先（用于儿童/老人/孕妇场景）

每个策略定义了各关键维度的 (min, max) 权重约束。
"""

from dataclasses import dataclass, field


@dataclass
class StrategyConstraint:
    """一条策略约束：定义关键维度的权重合法范围。

    每个维度有一个 (min, max) 区间。None 表示该方向无约束。
    """

    name: str
    """策略名称，如 'balanced' / 'safety_first' """

    constraints: dict[str, tuple[float | None, float | None]] = field(default_factory=dict)
    """维度约束映射：feature_name → (min_weight, max_weight)
    None 表示不设限。如 (0.25, None) 表示"至少 0.25，上不封顶" """

    def validate(self, weights: dict[str, float]) -> tuple[bool, str]:
        """用这个策略校验一组权重。

        Args:
            weights: 权重 dict，如 {"symptom_match": 0.30, "safety": 0.25, ...}

        Returns:
            (is_valid, reason)：
              - (True, "OK")               ← 通过
              - (False, "[balanced] safety=0.10 低于最小权重 0.20") ← 不通过
        """
        for feature_name, (min_w, max_w) in self.constraints.items():
            actual = weights.get(feature_name, 0.0)

            if min_w is not None and actual < min_w:
                return False, (
                    f"[{self.name}] {feature_name}={actual:.2f} "
                    f"低于最小权重 {min_w:.2f}"
                )
            if max_w is not None and actual > max_w:
                return False, (
                    f"[{self.name}] {feature_name}={actual:.2f} "
                    f"超过最大权重 {max_w:.2f}"
                )
        return True, "OK"


# ═══════════════════════════════════════════════════════════════
# 内置策略
# ═══════════════════════════════════════════════════════════════

BALANCED = StrategyConstraint(
    name="balanced",
    constraints={
        "symptom_match": (0.25, 0.35),    # 症状匹配占 25%-35%
        "safety": (0.20, 0.30),           # 安全占 20%-30%
        "age_suitability": (0.15, 0.25),  # 年龄适用性占 15%-25%
    },
)
"""均衡策略：症状匹配和安全同等重要。适用于大多数常规场景。"""

SAFETY_FIRST = StrategyConstraint(
    name="safety_first",
    constraints={
        "safety": (0.35, 0.50),           # 安全占 35%-50%（主导）
        "symptom_match": (0.10, 0.30),    # 症状匹配占 10%-30%（次要）
        "age_suitability": (0.15, 0.25),  # 年龄适用性占 15%-25%
    },
)
"""安全优先策略：安全权重主导。适用于儿童/老人/孕妇等高风险场景。"""


# ═══════════════════════════════════════════════════════════════
# 策略注册表
# ═══════════════════════════════════════════════════════════════

_BUILTIN_STRATEGIES: dict[str, StrategyConstraint] = {
    "balanced": BALANCED,
    "safety_first": SAFETY_FIRST,
}


class StrategyValidator:
    """用已注册的策略校验权重配置。

    使用方式：
        validator = StrategyValidator()
        ok, reason = validator.validate(weights, "balanced")
        if not ok:
            logger.warning(f"Config validation: {reason}")
    """

    def __init__(self):
        """初始化校验器，注册内置策略。"""
        self._strategies = dict(_BUILTIN_STRATEGIES)

    def validate(self, weights: dict[str, float], strategy_name: str) -> tuple[bool, str]:
        """用指定策略校验权重是否合法。

        Args:
            weights:       待校验的权重 dict
            strategy_name: 策略名称（如 'balanced' / 'safety_first'）

        Returns:
            (is_valid, reason)：
              - 策略名未知时 is_valid=False, reason 提示可用的策略名列表
              - 违反约束时 is_valid=False, reason 说明哪个维度不符合
              - 全部通过时 is_valid=True, reason="OK"
        """
        strategy = self._strategies.get(strategy_name)
        if strategy is None:
            return False, (
                f"Unknown strategy: {strategy_name}. "
                f"Available: {list(self._strategies.keys())}"
            )
        return strategy.validate(weights)

    def list_strategies(self) -> list[str]:
        """列出所有已注册的策略名称。"""
        return list(self._strategies.keys())
