"""
React node — SOP 管线 + ReactAgent fallback。

Skills 架构（v3）：
  SOP 管线（多数查询）:
    1. SkillRouter.route()       → task_type
    2. TaskClassifier.classify() → TaskClassification
    3. SOPEngine.execute()       → SOPResult
    4. ResponseGenerator.generate() → final_response

  ReAct fallback（闲聊/未分类/低置信度）:
    ReactAgent.run() → final_response

在 Graph 中位于三条路径：
  A. dispatcher → react → end（纯 react，跳过 workflow）
  B. consult → react → end（ask + react 混合意图）
  C. inventory → react → end（workflow done + react 混合意图）
"""

import logging

from app.agent.react.agent import ReactAgent
from app.agent.react.skills import (
    SOPEngine,
    SkillRouter,
    TaskClassifier,
    ResponseGenerator,
    TASK_SOP_MAP,
)
from app.agent.react.skills.types import TaskType
from app.graph.state import ConversationState, normalize_messages

logger = logging.getLogger(__name__)

# ── 需要 TaskClassifier 细分类的 intent ──
_CLASSIFY_INTENTS = {"ask_drug", None, ""}

# ── ReAct fallback 的 intent ──
_FALLBACK_INTENTS = {"chat", "give_up"}


async def react_node(
    state: ConversationState,
    react_agent: ReactAgent,
    state_proxy=None,
    skill_router: SkillRouter | None = None,
    sop_engine: SOPEngine | None = None,
    task_classifier: TaskClassifier | None = None,
    response_generator: ResponseGenerator | None = None,
) -> dict:
    """执行 ReactAgent（带 SOP 管线优化）。

    Args:
        state:              当前对话状态
        react_agent:        ReactAgent 实例（ReAct fallback 用）
        state_proxy:        _StateProxy 实例
        skill_router:       SkillRouter 实例
        sop_engine:         SOPEngine 实例
        task_classifier:    TaskClassifier 实例
        response_generator: ResponseGenerator 实例

    Returns:
        state 更新 dict：response, phase, node_events
    """
    # ── 0. 统一 normalize messages + 提取 query/intent ──
    raw_messages = state.get("messages", [])
    messages = normalize_messages(raw_messages)

    actions = state.get("dispatcher_result", {}).get("actions", [])
    react_actions = [a for a in actions if a.get("action") == "react"]
    intent = react_actions[0].get("intent", "") if react_actions else ""

    query = ""
    if react_actions:
        query = react_actions[0].get("query", "")
    if not query:
        for m in reversed(messages):
            if m.get("role") == "user":
                query = m.get("content", "")
                break

    # ── 1. 构建 workflow 上下文 ──
    recommendations = state.get("recommendations", [])
    consult_next_action = state.get("consult_next_action", "")
    current_phase = state.get("phase", "")

    if recommendations and current_phase in ("recommending", "ended"):
        workflow_action = "done"
    elif consult_next_action == "ask" and current_phase == "consulting":
        workflow_action = "ask"
    elif recommendations:
        workflow_action = "done"
    else:
        workflow_action = "ask"

    workflow_response = state.get("response", "")
    if not workflow_response and recommendations:
        drug_names = [
            r.get("generic_name", "") for r in recommendations[:3]
        ]
        workflow_response = f"系统已推荐：{'、'.join(drug_names)}"

    workflow_context = None
    if workflow_response or recommendations:
        workflow_context = {
            "workflow_action": workflow_action,
            "workflow_response": workflow_response,
        }

    # ── 2. 更新 state_proxy ──
    if state_proxy is not None:
        state_proxy.recommendations = recommendations
        slots = state.get("consult_slots", {})
        state_proxy.user_profile = {
            "age": slots.get("age"),
            "allergies": slots.get("allergies", []),
            "chronic_conditions": slots.get("chronic_conditions", []),
            "special_population": slots.get("special_population"),
        }

    # ── 3. 执行（SOP 管线 or ReAct fallback） ──
    has_recommendations = len(recommendations) > 0
    react_response = await _execute(
        query=query,
        history=messages,
        intent=intent,
        has_recommendations=has_recommendations,
        recommendations=recommendations,
        workflow_context=workflow_context,
        skill_router=skill_router,
        sop_engine=sop_engine,
        task_classifier=task_classifier,
        response_generator=response_generator,
        react_agent=react_agent,
    )

    # ── 4. 组装最终回复 ──
    previous_response = state.get("response", "")
    if previous_response and intent not in _FALLBACK_INTENTS:
        final_response = f"{previous_response}\n\n{react_response}"
    else:
        final_response = react_response

    # ── 5. 判断 phase ──
    if workflow_action == "ask" and current_phase == "consulting":
        phase = "consulting"
    elif state.get("phase") == "consulting":
        phase = "consulting"
    else:
        phase = "ended"

    return {
        "response": final_response,
        "phase": phase,
        "node_events": [{
            "node": "react",
            "intent": intent,
        }],
    }


# ═══════════════════════════════════════════════════════════════
# 核心执行逻辑
# ═══════════════════════════════════════════════════════════════


async def _execute(
    query: str,
    history: list[dict],
    intent: str,
    has_recommendations: bool,
    recommendations: list[dict],
    workflow_context: dict | None,
    skill_router: SkillRouter | None,
    sop_engine: SOPEngine | None,
    task_classifier: TaskClassifier | None,
    response_generator: ResponseGenerator | None,
    react_agent: ReactAgent,
) -> str:
    """执行查询——优先 SOP 管线，兜底 ReAct。

    决策树:
      1. chat/give_up → ReAct fallback（闲聊不需要分类）
      2. SkillRouter 直路由 → SOP 管线
      3. ask_drug → TaskClassifier 细分类 → SOP 管线
      4. 低置信度 / 分类失败 → ReAct fallback
    """
    # ── Fallback 直接返回 ──
    if intent in _FALLBACK_INTENTS or not query:
        return await _run_react_fallback(query, history, workflow_context, react_agent)

    # ── Step 1: SkillRouter 确定性路由 ──
    task_type = None
    classification = None  # 确保 SkillRouter 直路由时变量已定义
    if skill_router is not None:
        task_type = skill_router.route(
            intent=intent,
            query=query,
            has_recommendations=has_recommendations,
        )

    # ── Step 2: TaskClassifier 细分类（需要时） ──
    if task_type is None and task_classifier is not None:
        classification = await task_classifier.classify(
            query=query,
            history=history,
            context=_build_classify_context(recommendations, workflow_context),
        )
        # 低置信度 → ReAct fallback
        if classification.confidence < TaskClassifier.MIN_CONFIDENCE:
            logger.debug(
                "Low confidence (%.2f < %.2f), falling back to ReAct",
                classification.confidence,
                TaskClassifier.MIN_CONFIDENCE,
            )
            return await _run_react_fallback(query, history, workflow_context, react_agent)

        task_type = classification.task_type

    # ── Step 3: 获取 SOP 定义 ──
    if task_type is None or task_type not in TASK_SOP_MAP:
        return await _run_react_fallback(query, history, workflow_context, react_agent)

    sop = TASK_SOP_MAP[task_type]

    # ── Step 3.5: 构建 SOP 参数 ──
    #todo
    sop_params = _build_sop_params(
        query=query,
        task_type=task_type,
        classification=classification,
        recommendations=recommendations,
    )
    if sop_params is None:
        # 参数不足（如 drug_names 为空）→ ReAct fallback
        return await _run_react_fallback(query, history, workflow_context, react_agent)

    # ── Step 4: SOPEngine 执行 ──
    if sop_engine is None:
        return await _run_react_fallback(query, history, workflow_context, react_agent)

    sop_result = await sop_engine.execute(sop, sop_params)
    logger.debug(
        "SOP executed: task=%s, steps=%d, has_data=%s, web=%s",
        task_type,
        len(sop_result.steps),
        sop_result.has_usable_data,
        sop_result.triggered_web_fallback,
    )

    # ── Step 5: ResponseGenerator 生成回复 ──
    if response_generator is None:
        return await _run_react_fallback(query, history, workflow_context, react_agent)

    response = await response_generator.generate(
        query=query,
        sop_result=sop_result,
        sop=sop,
    )
    return response


# ═══════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════


async def _run_react_fallback(
    query: str,
    history: list[dict],
    workflow_context: dict | None,
    react_agent: ReactAgent,
) -> str:
    """回退到完整的 ReAct 循环（闲聊/未分类/低置信度）。"""
    logger.debug("Using ReAct fallback for query: %s", query[:50])
    result = await react_agent.run(
        user_message=query,
        history=history,
        context=workflow_context,
    )
    return result.final_response


def _build_classify_context(
    recommendations: list[dict],
    workflow_context: dict | None,
) -> dict:
    """构建传给 TaskClassifier 的上下文。"""
    ctx: dict = {}
    if recommendations:
        ctx["recommendations"] = recommendations
    if workflow_context:
        response = workflow_context.get("workflow_response", "")
        if response:
            ctx["workflow_response"] = response
    return ctx


def _build_sop_params(
    query: str,
    task_type: TaskType,
    classification,
    recommendations: list[dict],
) -> dict[str, str] | None:
    """从 TaskClassification 构建 SOP 执行参数。

    将 classification 中的语义信息转换为 SOP 的 args_template 占位符。

    Returns:
        参数字典，或 None（参数不足，应走 ReAct fallback）
    """
    params: dict[str, str] = {}
    drug_names = classification.drug_names if classification else []

    # ── 药品对比/相互作用：需要至少 2 个药 ──
    if task_type in (TaskType.DRUG_COMPARISON, TaskType.DRUG_INTERACTION):
        if len(drug_names) >= 2:
            params["drug_a"] = drug_names[0]
            params["drug_b"] = drug_names[1]
        elif len(drug_names) == 1:
            # 只有一个药名 → 尝试从推荐列表中补充
            rec_names = [
                r.get("generic_name", "")
                for r in recommendations[:2]
                if r.get("generic_name")
            ]
            if rec_names:
                params["drug_a"] = drug_names[0]
                params["drug_b"] = rec_names[0]
            else:
                # 只有一个药名且无推荐 → 走 ReAct fallback
                return None
        else:
            # 没有药名 → 用推荐列表
            rec_names = [
                r.get("generic_name", "")
                for r in recommendations[:2]
                if r.get("generic_name")
            ]
            if len(rec_names) >= 2:
                params["drug_a"] = rec_names[0]
                params["drug_b"] = rec_names[1]
            else:
                return None

    # ── 推荐解释 ──
    elif task_type == TaskType.RECOMMENDATION_EXPLANATION:
        sub_scene = classification.sub_scene if classification else ""
        if sub_scene == "why_not_recommend" and classification and classification.target_drug:
            params["target_drug"] = classification.target_drug
        # why_recommend / 默认：get_recommendation 不需要参数

    # ── 前 5 个简单类型：需要 1 个药名 ──
    else:
        if drug_names:
            params["drug_name"] = drug_names[0]
        else:
            # 尝试从推荐列表获取
            if recommendations:
                rec_name = recommendations[0].get("generic_name", "")
                if rec_name:
                    params["drug_name"] = rec_name
                else:
                    return None
            else:
                return None

        # 特殊人群：注入 population 参数
        if task_type == TaskType.SPECIAL_POPULATION and classification:
            params["population"] = classification.population or "特殊人群"
            # search_web 用
            params["web_query"] = params["drug_name"] + " " + params["population"] + " 用药安全"

    return params
