"""
ResponseGenerator — LLM #2: 把 SOP 结果变成自然语言回复。

职责非常窄：结构化数据 → 人话。不做任何决策。
强制性安全约束由代码注入到 prompt 中，不依赖 LLM 自觉。
"""

import logging
from typing import Any

from app.agent.react.skills.types import SOP, SOPResult
from app.llm.client import LLMClient
from app.llm.profile import LLMProfile

logger = logging.getLogger(__name__)

# ── 生成提示词 ───────────────────────────────────────────────

GENERATE_PROMPT = """你是 OTC 药店智能助手。基于以下查询结果回答用户问题。

⚠️ 核心约束：
- 只能使用下面「查询结果」中提供的信息，不得编造或补充
- 如果查询结果为空或不充分，诚实告知而非猜测
- 语言专业、清晰、亲切，像药店执业药师
- 涉及剂量、禁忌等重要信息时，强调"请以说明书为准"
- 不要提及"评分""排名""数据库"等系统内部概念

## 用户问题
{query}

## 查询结果
{formatted_data}

## 回复结构
{response_structure}

## 必须包含的提醒
{reminders}

## 来源标注规则
{source_rules}
"""

# ── 来源标注规则 ────────────────────────────────────────────

_LOCAL_SOURCE_RULES = (
    "本次回答的信息来自系统本地知识库。"
    "正常引用内容即可，不要标注「来源：xxx」、"
    "「根据数据库查询」等工具名或数据源名称。"
)

_WEB_SOURCE_RULES = (
    "本次回答的部分信息来自互联网搜索。"
    "在回复末尾单独标注「以上部分信息来自互联网搜索，仅供参考，"
    "请以药品说明书或医生/药师意见为准」。"
    "不要在每个信息点后逐一标注 URL。"
)


class ResponseGenerator:
    """LLM #2: 把结构化数据变成自然语言回复。

    职责非常窄：
      - 数据格式化（SOPResult → 文本）
      - LLM 回复生成（文本 → 自然语言）
    不做任何决策。

    使用方式：
        generator = ResponseGenerator(llm_client, profile)
        response = await generator.generate(
            query="布洛芬有什么副作用",
            sop_result=result,
            sop=SIDE_EFFECTS_SOP,
        )
    """

    def __init__(
        self,
        llm_client: LLMClient,
        profile: LLMProfile | None = None,
    ):
        """初始化回复生成器。

        Args:
            llm_client: LLM 客户端
            profile:    回复生成场景的 LLMProfile。
                        None 时使用默认 profile。
        """
        self._llm = llm_client
        self._profile = profile

    async def generate(
        self,
        query: str,
        sop_result: SOPResult,
        sop: SOP,
    ) -> str:
        """将 SOP 执行结果转为自然语言回复。

        Args:
            query:      用户原始问题
            sop_result: SOP 执行结果
            sop:        SOP 定义（含回复结构、安全约束、兜底模板）

        Returns:
            用户可见的最终回复文本
        """
        # 全部无数据 → 返回 SOP 兜底模板
        if not sop_result.has_usable_data:
            return sop.fallback_response

        # 格式化数据
        formatted = self._format_data(sop_result)

        # 构建 prompt
        reminders = self._build_reminders(sop)
        source_rules = (
            _WEB_SOURCE_RULES
            if sop_result.triggered_web_fallback
            else _LOCAL_SOURCE_RULES
        )

        prompt = GENERATE_PROMPT.format(
            query=query,
            formatted_data=formatted,
            response_structure=sop.response_structure,
            reminders=reminders,
            source_rules=source_rules,
        )

        try:
            response = await self._llm.generate(
                messages=[{"role": "user", "content": prompt}],
                profile=self._profile,
            )
            content = response.get("choices", [{}])[0].get("message", {}).get("content", "")
            return content or sop.fallback_response

        except Exception as e:
            logger.error("ResponseGenerator LLM call failed: %s", e)
            # LLM 不可用 → 返回原始数据摘要
            return self._raw_fallback(query, sop_result)

    # ── 内部方法 ────────────────────────────────────────────

    def _format_data(self, result: SOPResult) -> str:
        """将 SOPResult 中的数据序列化为 LLM 可读的文本。

        按步骤顺序排列，标注每步的数据来源。
        """
        parts: list[str] = []
        for step in result.steps:
            if not step.success or step.data is None:
                continue

            label = self._step_label(step.tool_name)
            content = self._format_step_data(step.data)
            if content:
                parts.append(f"### {label}\n{content}")

        return "\n\n".join(parts) if parts else "（未获取到任何数据）"

    @staticmethod
    def _step_label(tool_name: str) -> str:
        """将工具名映射为人类可读的标签。"""
        labels = {
            "search_manual": "说明书检索结果",
            "get_drug_detail": "药品档案信息",
            "get_drug_detail_backup": "药品档案信息",
            "search_web": "联网搜索结果",
            "search_drug": "药品匹配结果",
            "get_recommendation": "系统推荐列表",
            "get_user_profile": "用户信息",
        }
        return labels.get(tool_name, tool_name)

    @staticmethod
    def _format_step_data(data: Any) -> str:
        """将单步工具返回的 data 格式化为文本。"""
        if isinstance(data, list):
            lines: list[str] = []
            for item in data:
                if isinstance(item, dict):
                    content = item.get("content", "")
                    source = item.get("source", "")
                    drug = item.get("drug_name", "")
                    title = item.get("title", "")

                    if title:
                        lines.append(f"**{title}**")
                    if drug:
                        lines.append(f"【{drug}】")
                    if content:
                        lines.append(content)
                    if source and source != "local":
                        lines.append(f"（来源：{source}）")
                else:
                    lines.append(str(item))
            return "\n".join(lines)

        elif isinstance(data, dict):
            lines: list[str] = []
            # 对 get_drug_detail 的结构化 dict 做格式化
            field_labels = {
                "generic_name": "药品名称",
                "trade_names": "商品名",
                "category": "类别",
                "indications": "适应症",
                "usage_dosage": "用法用量",
                "adverse_reactions": "不良反应",
                "contraindications": "禁忌",
                "interactions": "药物相互作用",
                "precautions": "注意事项",
            }
            for key, value in data.items():
                if key in ("drug_id", "found", "source", "error", "empty", "message"):
                    continue
                if value and isinstance(value, str) and len(value) >= 3:
                    label = field_labels.get(key, key)
                    lines.append(f"**{label}**：{value}")
                elif isinstance(value, list) and value:
                    label = field_labels.get(key, key)
                    lines.append(f"**{label}**：{'、'.join(str(v) for v in value)}")

            # search_web 结果
            if data.get("source") == "web":
                results = data.get("results", [])
                if results:
                    for r in results:
                        lines.append(f"- {r.get('title', '')}: {r.get('snippet', '')}")
                        if r.get("url"):
                            lines.append(f"  URL: {r.get('url')}")

            return "\n".join(lines) if lines else ""

        return str(data) if data else ""

    @staticmethod
    def _build_reminders(sop: SOP) -> str:
        """构建强制性提醒文本。"""
        if not sop.mandatory_reminders:
            return "（无特殊提醒）"
        return "\n".join(f"- {r}" for r in sop.mandatory_reminders)

    def _raw_fallback(self, query: str, result: SOPResult) -> str:
        """LLM 不可用时的原始数据摘要。"""
        formatted = self._format_data(result)
        if not formatted.strip():
            return "抱歉，当前服务暂时不可用。建议查看药品纸质说明书，或咨询医生/药师。"
        return f"""根据查询到的信息：

{formatted}

（以上信息由系统自动查询整理，如需更详细的信息，建议咨询医生或药师。）"""
