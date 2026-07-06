"""
TaskClassifier — LLM #1: 语义分类 + 参数提取。

职责非常窄：把自然语言 query 归类为 TaskType + 提取关键参数。
不参与任何执行决策——仅做语义理解。

独立组件，不依赖 Skill / SOP / ReactAgent。
"""

import logging
from typing import Any

from app.agent.react.skills.types import TaskClassification, TaskType
from app.llm.client import LLMClient
from app.llm.profile import LLMProfile

logger = logging.getLogger(__name__)

# ── 分类提示词 ───────────────────────────────────────────────

CLASSIFY_PROMPT = """你是药品查询意图分类器。分析用户问题，输出分类结果和提取的参数。

## 任务类型

| 类型 | 适用场景 | 示例 |
|------|---------|------|
| side_effects | 询问副作用/不良反应/吃了会有什么反应 | "布洛芬有什么副作用""吃了会头晕吗" |
| contraindications | 询问禁忌/什么人不能吃/特定疾病能否服用 | "有胃溃疡能吃布洛芬吗""什么人不能吃" |
| dosage | 询问用法用量/怎么吃/剂量/饭前还是饭后 | "布洛芬怎么吃""儿童用量多少" |
| efficacy | 询问功效/适应症/能治什么/有什么作用 | "布洛芬有什么作用""这个药能治头痛吗" |
| special_population | 孕妇/哺乳期/儿童/老人用药安全性 | "孕妇能吃对乙酰氨基酚吗""哺乳期能用吗" |
| drug_interaction | 药物能否一起吃/是否有相互作用/冲突 | "布洛芬和头孢能一起吃吗""这两个有冲突吗" |
| drug_comparison | 药品对比/哪个更好/有什么区别 | "布洛芬和对乙酰氨基酚哪个好""有什么区别" |
| recommendation_explanation | 询问为什么推荐某药/为什么不推荐某药 | "为什么推荐布洛芬""怎么不推荐XX" |

## 分类指南

1. **优先识别 special_population**：如果用户明确提到了孕妇/哺乳期/儿童/老人，即使问的是副作用，也归类为 special_population——因为特殊人群的用药信息需要不同的安全标准。

2. **副作用 vs 禁忌**：
   - "吃了会有什么反应" → side_effects
   - "有胃溃疡能不能吃" → contraindications（虽然涉及负面反应，但用户问的是"能否"）

3. **功效 vs 对比**：
   - "布洛芬有什么作用" → efficacy
   - "布洛芬和对乙酰氨基酚哪个效果好" → drug_comparison

4. **相互作用识别**：涉及 2 个或以上药品，且问"能不能一起吃""有没有冲突""会相互作用吗" → drug_interaction

5. **推荐解释识别**：涉及"为什么推荐""为什么不推荐""怎么不推荐""为啥推荐"等关键词 → recommendation_explanation

## 参数提取

- **drug_names**：从 query 和对话历史中提取所有涉及的药品通用名。如果用户用指代词（"这个药"），从对话历史中尝试解析。最多提取 5 个。
- **population**：仅 special_population 类型需要。从 query 中提取：孕妇 / 哺乳期 / 儿童 / 老人。如果用户没有明确提及特殊人群，填 null。
- **custom_focus**：用户特别关心的具体点（如"对肝脏的影响""会不会影响睡眠""饭前还是饭后"）。普通查询填 null。
- **sub_scene**：仅 recommendation_explanation 类型需要。"why_recommend"（为什么推荐）/ "why_not_recommend"（为什么不推荐）
- **target_drug**：仅 why_not_recommend 子场景需要。用户问"为什么不推荐 XX"中的 XX。
- **confidence**：0.0-1.0。如果你对分类不确定（query 含糊或同时匹配多个类型），降低 confidence。0.7 以下会被系统降级为通用处理。

## 对话历史使用
- 仅用于解析指代（"这个药" → 从历史中获取药名）
- 不根据历史推断答案
- 如果对话历史中有系统推荐了药品，且用户说"这些药有什么作用"，drug_names 应从推荐列表中获取

## 输出格式
严格输出 JSON。"""
# ── 分类器 ──────────────────────────────────────────────────


class TaskClassifier:
    """LLM #1: 将自然语言 query 分类为 TaskType + 提取参数。

    独立组件 — 不依赖 Skill、SOP 或 ReactAgent。
    仅负责语义理解（分类 + 参数提取），不参与任何执行决策。

    使用方式：
        classifier = TaskClassifier(llm_client, profile)
        result = await classifier.classify(
            query="布洛芬有什么副作用",
            history=[...],
            context={"recommendations": [...]},
        )
        # result.task_type == TaskType.SIDE_EFFECTS
        # result.drug_names == ["布洛芬"]
    """

    # 低于此置信度的分类结果走 ReAct fallback
    MIN_CONFIDENCE = 0.7

    def __init__(
        self,
        llm_client: LLMClient,
        profile: LLMProfile | None = None,
    ):
        """初始化分类器。

        Args:
            llm_client: LLM 客户端
            profile:    分类场景的 LLMProfile（低 temperature 以保证一致性）。
                        None 时使用默认 profile。
        """
        self._llm = llm_client
        self._profile = profile

    async def classify(
        self,
        query: str,
        history: list[dict[str, str]] | None = None,
        context: dict[str, Any] | None = None,
    ) -> TaskClassification:
        """分类用户查询 + 提取参数。

        Args:
            query:   用户当前输入文本
            history: 对话历史（最近几轮）。用于解析指代。
            context: 上下文信息。包含 recommendations（推荐列表）等，
                     用于辅助分类和参数提取。

        Returns:
            TaskClassification。如果 confidence < 0.7，调用方应走 ReAct fallback。
        """
        messages: list[dict[str, str]] = [
            {"role": "system", "content": CLASSIFY_PROMPT},
        ]

        # 对话历史（最近 6 条，帮助解析指代）
        if history:
            normalized = _normalize(history)
            messages.extend(normalized[-6:])

        # 上下文信息注入
        context_text = _build_context_injection(context)
        user_content = query
        if context_text:
            user_content = context_text + "\n\n用户问题：" + query

        messages.append({"role": "user", "content": user_content})

        try:
            result = await self._llm.generate_structured(
                messages=messages,
                schema=TaskClassification,
                profile=self._profile,
            )
            logger.debug(
                "Classified query as %s (confidence=%.2f), drugs=%s",
                result.task_type,
                result.confidence,
                result.drug_names,
            )
            return result

        except Exception as e:
            logger.warning("TaskClassifier failed: %s, falling back", e)
            return TaskClassification(
                task_type=TaskType.EFFICACY,  # 安全兜底：默认为功效查询
                drug_names=[],
                confidence=0.0,
            )


# ── 辅助 ─────────────────────────────────────────────────────


# LangChain → OpenAI 角色名映射（与 app/agent/react/agent.py 和 app/graph/state.py 保持一致）
_LC_ROLE_MAP = {
    "human": "user",
    "user": "user",
    "ai": "assistant",
    "assistant": "assistant",
    "system": "system",
    "tool": "tool",
    "function": "function",
}


def _normalize(history: list[dict[str, str]]) -> list[dict[str, str]]:
    """标准化历史消息为 {role, content} 格式。

    LangGraph 的 checkpoint 可能把消息存为 LangChain 对象，
    需要统一转为 {"role": "...", "content": "..."} 格式，
    并将 LangChain 角色名（human/ai）映射为 OpenAI 标准角色名（user/assistant）。
    """
    result: list[dict[str, str]] = []
    for msg in history:
        if not isinstance(msg, dict):
            role = getattr(msg, "role", None) or getattr(msg, "type", "user")
            role = _LC_ROLE_MAP.get(role, role)
            content = getattr(msg, "content", "")
            result.append({"role": role, "content": str(content)})
        else:
            role = msg.get("role", "user")
            role = _LC_ROLE_MAP.get(role, role)
            result.append({
                "role": role,
                "content": str(msg.get("content", "")),
            })
    return result


def _build_context_injection(context: dict[str, Any] | None) -> str:
    """构建上下文注入文本（注入到 user message 中）。

    帮助 TaskClassifier 理解当前对话状态，更准确地分类和提取参数。
    """
    if not context:
        return ""

    parts: list[str] = []

    recommendations = context.get("recommendations", [])
    if recommendations:
        drug_names = [
            r.get("generic_name", "")
            for r in recommendations[:3]
            if r.get("generic_name")
        ]
        if drug_names:
            parts.append(f"系统当前推荐的药品：{'、'.join(drug_names)}")

    consult_summary = context.get("consult_summary", "")
    if consult_summary:
        parts.append(f"用户症状摘要：{consult_summary}")

    return "\n".join(parts) if parts else ""
