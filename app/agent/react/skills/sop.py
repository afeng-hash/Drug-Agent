"""
SOPEngine — 代码层确定性执行工具链。

按 SOP 定义的步骤执行工具调用。LLM 零参与——流程由代码控制。

数据充分性判断：
  Step 1 + Step 2 总是执行（search_manual + get_drug_detail 互补）
  Step 3 (search_web) 仅当本地源全空时触发

原则：不做"内容质量"判断，只做"内容有无"判断。
"""

import asyncio
import logging
from typing import Any

from app.agent.react.skills.types import (
    SOP,
    SOPResult,
    StepResult,
    TaskType,
)
from app.agent.react.tools import ToolRegistry

logger = logging.getLogger(__name__)

# ── 可配置阈值 ───────────────────────────────────────────────

_MIN_SEARCH_MANUAL_CHARS = 50
"""search_manual 返回内容的总字符数低于此值 → 视为无有效数据"""

_MIN_DB_FIELD_CHARS = 10
"""get_drug_detail 返回的字段内容低于此值 → 该字段视为空"""

_SKIP_KEYS = frozenset({"drug_id", "generic_name", "trade_names", "category", "found"})
"""get_drug_detail 中跳过元数据字段，只看实质性内容字段"""


# ── 辅助函数 ────────────────────────────────────────────────


def _has_usable_data(results: list[Any]) -> bool:
    """检查数据列表中是否至少有一个返回了有效信息。

    不判断内容质量——只判断"有没有东西"。
    阈值极低（50字 / 10字），仅筛掉完全空的或只有元数据的返回。

    Args:
        results: 工具返回的数据列表（search_manual 返回 list[dict]，
                 get_drug_detail 返回 dict）

    Returns:
        True 如果至少一个结果包含有效内容，False 如果全部为空
    """
    for data in results:
        if data is None:
            continue

        # search_manual / search_drug 结果: list[dict]
        if isinstance(data, list):
            total_len = sum(
                len(item.get("content", "")) if isinstance(item, dict) else 0
                for item in data
            )
            if total_len >= _MIN_SEARCH_MANUAL_CHARS:
                return True

        # get_drug_detail / search_web 结果: dict
        elif isinstance(data, dict):
            # 显式错误 → 视为无数据
            if data.get("error"):
                continue
            # 显式空标记
            if data.get("empty") is True or data.get("found") is False:
                continue
            # search_web: 有 results 且非空
            if data.get("source") == "web":
                web_results = data.get("results", [])
                if isinstance(web_results, list) and len(web_results) > 0:
                    return True
                continue
            # get_drug_detail: 检查实质性字段
            meaningful_fields = [
                v
                for k, v in data.items()
                if k not in _SKIP_KEYS
                and v
                and isinstance(v, str)
                and len(v) >= _MIN_DB_FIELD_CHARS
            ]
            if meaningful_fields:
                return True

    return False


def _fill_template(template: dict[str, str], params: dict[str, str]) -> dict[str, Any]:
    """填充参数模板中的占位符。

    例如 template={"drug_name": "{drug_name}", "question": "副作用"}
          params={"drug_name": "布洛芬"}
        → {"drug_name": "布洛芬", "question": "副作用"}

    {drug_name} / {population} / {custom_focus} / {target_drug} 等占位符会被替换。
    模板中不含占位符的值原样保留。
    """
    result: dict[str, Any] = {}
    for key, value in template.items():
        if isinstance(value, str):
            # 替换所有 {xxx} 占位符
            filled = value
            for p_key, p_val in params.items():
                if p_val:
                    filled = filled.replace("{" + p_key + "}", str(p_val))
            # 尝试把数字字符串转回 int（如 top_k / num_results）
            if isinstance(filled, str) and filled.isdigit():
                result[key] = int(filled)
            else:
                result[key] = filled
        else:
            result[key] = value
    return result


# ── SOP 执行引擎 ────────────────────────────────────────────


class SOPEngine:
    """按 SOP 确定性执行工具链。

    使用方式：
        engine = SOPEngine(tool_registry)
        sop = SIDE_EFFECTS_SOP  # 从 task_definitions 获取
        params = {"drug_name": "布洛芬", "question": "副作用 不良反应"}
        result = await engine.execute(sop, params)
        # result.steps → 各步结果
        # result.has_usable_data → 是否拿到了数据
    """

    def __init__(self, tool_registry: ToolRegistry):
        """初始化执行引擎。

        Args:
            tool_registry: 工具注册中心（已注册所有 7 个工具）
        """
        self._registry = tool_registry

    async def execute(self, sop: SOP, params: dict[str, str]) -> SOPResult:
        """按 SOP 执行工具链。

        search_web 在其他步骤之后执行，且仅当本地数据源全部空时触发。

        Args:
            sop:    任务类型对应的 SOP 定义
            params: 已填充占位符的参数字典。
                    如 {"drug_name": "布洛芬", "population": "孕妇"}

        Returns:
            SOPResult — 包含所有步骤结果 + 聚合元数据
        """
        all_results: list[StepResult] = []
        all_data: list[Any] = []

        # ── 1. 拆分本地步骤与联网步骤 ──
        web_step = self._find_web_step(sop)
        local_steps = [s for s in sop.steps if s.tool_name != "search_web"]

        # ── 2. 按并行组分组执行本地步骤 ──
        groups = self._group_by_parallel(local_steps)

        for group_idx, group in enumerate(groups):
            # 同组 step 并行执行
            group_results = await self._execute_group(group, params)
            all_results.extend(group_results)
            for gr in group_results:
                if gr.success and gr.data is not None:
                    all_data.append(gr.data)

        # ── 3. 判断是否需要联网兜底 ──
        triggered_web = False
        has_usable = _has_usable_data(all_data)

        if not has_usable and web_step is not None:
            # 本地源全空 → 触发联网兜底
            web_result = await self._execute_step(web_step, params)
            all_results.append(web_result)
            if web_result.success and web_result.data is not None:
                all_data.append(web_result.data)
            triggered_web = True

            # 检查联网是否救回来了
            has_usable = _has_usable_data(all_data)

        return SOPResult(
            task_type=sop.task_type,
            steps=all_results,
            has_usable_data=has_usable,
            triggered_web_fallback=triggered_web,
        )

    # ── 内部方法 ────────────────────────────────────────────

    def _group_by_parallel(self, steps: list) -> list[list]:
        """按 parallel_group 分组。同 group 的 step 并行执行。"""
        if not steps:
            return []

        groups: list[list] = []
        current_group: list = []

        for step in steps:
            if step.parallel_group == 0:
                # 0 表示与上一步不同组（顺序执行），单独成组
                if current_group:
                    groups.append(current_group)
                    current_group = []
                groups.append([step])
            else:
                # 属于同一并行组的 step 放一起
                if current_group and step.parallel_group == current_group[0].parallel_group:
                    current_group.append(step)
                else:
                    if current_group:
                        groups.append(current_group)
                    current_group = [step]

        if current_group:
            groups.append(current_group)

        return groups

    async def _execute_group(
        self, group: list, params: dict[str, str]
    ) -> list[StepResult]:
        """并行执行同组的所有 step。"""
        if len(group) == 1:
            result = await self._execute_step(group[0], params)
            return [result]

        # 并行执行
        tasks = [self._execute_step(step, params) for step in group]
        return list(await asyncio.gather(*tasks))

    async def _execute_step(
        self, step, params: dict[str, str]
    ) -> StepResult:
        """执行单个 SOPStep。填充参数模板 → 调用工具 → 返回 StepResult。"""
        filled_args = _fill_template(step.args_template, params)

        try:
            result = await self._registry.execute(step.tool_name, filled_args)
            return StepResult(
                step_order=step.order,
                tool_name=step.tool_name,
                success=result.success,
                data=result.data if result.success else None,
                error=result.error if not result.success else None,
            )
        except Exception as e:
            logger.error(
                "SOP step %d (%s) unexpected error: %s",
                step.order,
                step.tool_name,
                e,
            )
            return StepResult(
                step_order=step.order,
                tool_name=step.tool_name,
                success=False,
                error=str(e),
            )

    @staticmethod
    def _find_web_step(sop: SOP):
        """查找 SOP 中的 search_web 步骤（联网兜底步骤）。"""
        for step in sop.steps:
            if step.tool_name == "search_web":
                return step
        return None
