"""
SkillRouter — 意图 → task_type 确定性路由。

v1: 基于 dispatcher intent + 上下文检测的确定性路由表。零 LLM 开销。
    上下文感知：检测 query 中的推荐解释关键词 → 绕过 intent 直路由到 recommendation_explanation。
"""

from app.agent.react.skills.types import TaskType


# ── 推荐解释检测关键词 ──────────────────────────────────────

_RECOMMENDATION_KEYWORDS = [
    "为什么推荐", "为什么不推荐", "怎么不推荐", "为啥推荐",
    "为什么选", "为什么没", "怎么没有", "为啥没推荐",
    "推荐理由", "推荐依据", "为什么是", "怎么没推荐",
]


# ── intent → task_type 映射表 ───────────────────────────────

INTENT_TO_TASK: dict[str, TaskType | None] = {
    # 药品信息类 → 需要 TaskClassifier 细分类（side_effects/contraindications/...）
    "ask_drug": None,
    # 明确可直路由的类型
    "compare_drugs": TaskType.DRUG_COMPARISON,
    "ask_interaction": TaskType.DRUG_INTERACTION,
    # 闲聊/放弃 → ReAct fallback
    "chat": None,
    "give_up": None,
}


class SkillRouter:
    """意图 → task_type 的确定性路由。

    v1:
      - compare_drugs / ask_interaction → 直路由
      - recommendation 上下文检测 → 直路由
      - ask_drug → 返回 None，交给 TaskClassifier 细分类
      - chat / give_up → 返回 None，走 ReAct fallback
      - 未匹配 → 返回 None，走 TaskClassifier 再分类

    使用方式：
        router = SkillRouter()
        task_type = router.route(
            intent="ask_drug",
            query="布洛芬有什么副作用",
            has_recommendations=True,
        )
        # task_type is None → 需要 TaskClassifier 细分类
        # (然后 TaskClassifier 会返回 SIDE_EFFECTS)
    """

    def route(
        self,
        intent: str,
        query: str,
        has_recommendations: bool = False,
    ) -> TaskType | None:
        """路由 query 到 task_type。

        Args:
            intent:              dispatcher 输出的 intent 标签
            query:               用户当前输入文本
            has_recommendations: 当前 state 中是否有推荐列表

        Returns:
            TaskType — 明确匹配的任务类型
            None    — 需要交给 TaskClassifier 进一步判断
                      （或走 ReAct fallback，取决于调用方逻辑）
        """
        # ── 优先级 1: 上下文检测 — 推荐解释 ──
        if has_recommendations and self._is_recommendation_query(query):
            return TaskType.RECOMMENDATION_EXPLANATION

        # ── 优先级 2: 确定性 intent 映射 ──
        if intent in INTENT_TO_TASK:
            return INTENT_TO_TASK[intent]

        # ── 优先级 3: 未匹配 → 交给下游 ──
        return None

    @staticmethod
    def _is_recommendation_query(query: str) -> bool:
        """检测 query 是否涉及推荐解释。"""
        return any(kw in query for kw in _RECOMMENDATION_KEYWORDS)
