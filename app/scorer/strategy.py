"""
StrategyValidator — 校验权重/指数配置是否符合预设策略的约束。

为什么需要策略约束？
  权重配置是从数据库动态加载的，可以被运营人员修改。如果没有约束，
  可能会配出"安全权重=0"或"症状匹配权重=0.9"等不合理配置。

  StrategyValidator 在每次评分前校验配置是否落在预设的合理范围内。
  校验失败只记录警告日志，不阻塞推荐流程（防止配置错误导致服务不可用）。

支持两个评分版本：
  v1 — 几何加权平均，校验权重范围
  v2 — 层级乘法模型，校验指数范围

四种内置策略（v1 + v2 各两种）：
  1. balanced_v1      — 症状和安全同等重要
  2. safety_first_v1  — 安全优先
  3. balanced_v2      — 层级乘法，focus sqrt 附近
  4. safety_first_v2  — 层级乘法，年龄惩罚加码
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
# v1 内置策略（几何加权平均）
# ═══════════════════════════════════════════════════════════════

BALANCED_V1 = StrategyConstraint(
    name="balanced",
    constraints={
        "symptom_match": (0.40, 0.55),          # 症状匹配占 40%-55%（主排序信号）
        "symptom_focus_ratio": (0.10, 0.20),     # 集合覆盖比占 10%-20%
        "age_suitability": (0.20, 0.30),         # 年龄适用性占 20%-30%
    },
)
"""v1 均衡策略：症状匹配主导，年龄适用性提供软惩罚。"""

SAFETY_FIRST_V1 = StrategyConstraint(
    name="safety_first",
    constraints={
        "age_suitability": (0.35, 0.50),         # 年龄适用性占 35%-50%（主导）
        "symptom_match": (0.20, 0.35),           # 症状匹配占 20%-35%（次要）
        "symptom_focus_ratio": (0.05, 0.15),     # 集合覆盖比占 5%-15%
    },
)
"""v1 安全优先策略：年龄适用性权重主导。"""


# ═══════════════════════════════════════════════════════════════
# v2 内置策略（层级乘法模型）
# ═══════════════════════════════════════════════════════════════

BALANCED_V2 = StrategyConstraint(
    name="balanced",
    constraints={
        "focus": (0.3, 0.7),         # sqrt 附近浮动（0.3=轻度惩罚, 0.7=重度惩罚）
        "age": (0.15, 0.40),         # 软惩罚范围（0.15=微弱, 0.40=明显）
        "otc": (0.0, 0.10),          # 极弱，仅 tiebreaker
    },
)
"""v2 均衡策略：symptom_match 指数固定 1.0（不在约束中），focus/age/otc 独立调节。"""

SAFETY_FIRST_V2 = StrategyConstraint(
    name="safety_first",
    constraints={
        "age": (0.40, 0.70),         # 年龄惩罚加码（儿童/老人/孕妇场景）
        "focus": (0.15, 0.45),       # 纯度惩罚放松（更关注安全性而非专药性）
        "otc": (0.0, 0.15),          # OTC 也可适度参与
    },
)
"""v2 安全优先策略：年龄惩罚加码，纯度惩罚放松。"""


# ═══════════════════════════════════════════════════════════════
# 策略注册表
# ═══════════════════════════════════════════════════════════════

_BUILTIN_STRATEGIES_V1: dict[str, StrategyConstraint] = {
    "balanced": BALANCED_V1,
    "safety_first": SAFETY_FIRST_V1,
}

_BUILTIN_STRATEGIES_V2: dict[str, StrategyConstraint] = {
    "balanced": BALANCED_V2,
    "safety_first": SAFETY_FIRST_V2,
}


class StrategyValidator:
    """用已注册的策略校验权重/指数配置。

    使用方式：
        validator = StrategyValidator()
        ok, reason = validator.validate(weights, "balanced", scoring_version="v2")
        if not ok:
            logger.warning(f"Config validation: {reason}")
    """

    def __init__(self):
        """初始化校验器，注册 v1 + v2 内置策略。"""
        self._strategies_v1 = dict(_BUILTIN_STRATEGIES_V1)
        self._strategies_v2 = dict(_BUILTIN_STRATEGIES_V2)

    def validate(
        self,
        weights: dict[str, float],
        strategy_name: str,
        scoring_version: str = "v1",
    ) -> tuple[bool, str]:
        """用指定策略校验配置是否合法。

        Args:
            weights:         待校验的配置 dict（v1=权重, v2=指数）
            strategy_name:   策略名称（如 'balanced' / 'safety_first'）
            scoring_version: 评分公式版本 "v1" | "v2"

        Returns:
            (is_valid, reason)：
              - 策略名未知时 is_valid=False, reason 提示可用的策略名列表
              - 违反约束时 is_valid=False, reason 说明哪个维度不符合
              - 全部通过时 is_valid=True, reason="OK"
        """
        strategies = self._strategies_v2 if scoring_version == "v2" else self._strategies_v1
        strategy = strategies.get(strategy_name)
        if strategy is None:
            all_names = set(self._strategies_v1.keys()) | set(self._strategies_v2.keys())
            return False, (
                f"Unknown strategy: {strategy_name}. "
                f"Available: {sorted(all_names)}"
            )
        return strategy.validate(weights)

    def list_strategies(self, scoring_version: str = "v1") -> list[str]:
        """列出指定版本的所有已注册策略名称。"""
        strategies = self._strategies_v2 if scoring_version == "v2" else self._strategies_v1
        return list(strategies.keys())
