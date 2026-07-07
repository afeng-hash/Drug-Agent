"""
Graph builder — 组装 LangGraph 状态机（v2）。

图结构：
  intake → dispatcher ──→ consult ──→ safety ──→ recommend → inventory ──→ react → end
                    │        │          │                       │
                    │        │(ask)     │(BLOCK)                │(no react)
                    │        ├── react  └── end                 └── end
                    │        └── end
                    │
                    └── react → end

explain 节点被 react 替代——ReactAgent 工具驱动处理所有泛咨询。
consult / safety / recommend / inventory 保持为独立图节点（不变）。
"""

from functools import partial

from langgraph.graph import END, StateGraph

from app.agent.prompts import REACT_SYSTEM_PROMPT
from app.agent.react.agent import ReactAgent
from app.agent.react.skills import (
    SOPEngine,
    SkillRouter,
    TaskClassifier,
    ResponseGenerator,
    TASK_SOP_MAP,
)
from app.agent.react.tools import ToolRegistry
from app.agent.react.tools.base import BaseTool
from app.agent.react.tools.get_drug_detail import GetDrugDetailTool
from app.agent.react.tools.get_recommendation import GetRecommendationTool
from app.agent.react.tools.get_user_profile import GetUserProfileTool
from app.agent.react.tools.search_drug import SearchDrugTool
from app.agent.react.tools.search_manual import SearchManualTool
from app.agent.react.tools.search_web import SearchWebTool
from app.config import Settings
from app.graph.nodes.consult import consult_node
from app.search.service import TavilySearchService
from app.graph.nodes.dispatcher import dispatcher_node
from app.graph.nodes.end import end_node
from app.graph.nodes.intake import intake_node
from app.graph.nodes.inventory import inventory_node
from app.graph.nodes.react import react_node
from app.graph.nodes.recommend import recommend_node
from app.graph.nodes.safety_check import safety_block_node
from app.graph.router import (
    route_after_consult,
    route_after_dispatcher,
    route_after_inventory,
    route_after_safety,
)
from app.graph.state import ConversationState
from app.llm.client import LLMClient
from app.llm.profile import LLMProfile
from app.rag.retriever import DrugManualRetriever
from app.rules.engine import RuleEngine
from app.scorer.pipeline import ScoringPipeline


def build_graph(
    llm_client: LLMClient,
    rule_engine: RuleEngine,
    drug_repo_factory,
    inventory_repo_factory,
    session_repo_factory,
    safety_log_repo_factory,
    weight_repo_factory,
    retriever: DrugManualRetriever,
    scoring_pipeline: ScoringPipeline,
    drug_graph_repo=None,
    vocab_source=None,
    react_profile: LLMProfile | None = None,
    max_consult_rounds: int = 6,
) -> StateGraph:
    """构建并编译 LangGraph 状态机。

    Args:
        llm_client:           LLM 客户端
        rule_engine:          安全规则引擎
        drug_repo_factory:    药品仓库工厂
        inventory_repo_factory: 库存仓库工厂
        session_repo_factory: 会话仓库工厂
        safety_log_repo_factory: 安全日志仓库工厂
        weight_repo_factory:  权重配置仓库工厂
        retriever:            Milvus 药品说明书检索器
        scoring_pipeline:     评分排序管线
        drug_graph_repo:      Neo4j 图谱仓库（可选）
        vocab_source:         症状词表（可选）
        react_profile:        ReactAgent 的 LLMProfile
        max_consult_rounds:   问诊最大追问轮数

    Returns:
        编译好的 LangGraph 图
    """
    graph = StateGraph(ConversationState)

    # ── 添加节点 ──────────────────────────────────────────

    graph.add_node("intake", intake_node)

    graph.add_node("dispatcher", partial(dispatcher_node, llm_client=llm_client))

    graph.add_node(
        "consult",
        partial(consult_node, llm_client=llm_client, max_rounds=max_consult_rounds),
    )

    graph.add_node(
        "safety_block",
        partial(safety_block_node, rule_engine=rule_engine),
    )

    graph.add_node(
        "recommend",
        _make_recommend(
            llm_client, drug_repo_factory, weight_repo_factory,
            retriever, scoring_pipeline, drug_graph_repo, vocab_source,
        ),
    )

    graph.add_node(
        "inventory",
        _make_inventory(inventory_repo_factory, drug_repo_factory),
    )

    graph.add_node(
        "react",
        _make_react(
            llm_client, drug_repo_factory, retriever, react_profile,
        ),
    )

    graph.add_node(
        "end",
        _make_end(session_repo_factory, safety_log_repo_factory),
    )

    # ── 添加边 ──────────────────────────────────────────

    graph.set_entry_point("intake")
    graph.add_edge("intake", "dispatcher")

    # dispatcher → consult / react
    graph.add_conditional_edges(
        "dispatcher",
        route_after_dispatcher,
        {
            "consult": "consult",
            "react": "react",
        },
    )

    # consult → safety_block / react / end
    graph.add_conditional_edges(
        "consult",
        route_after_consult,
        {
            "safety_block": "safety_block",
            "react": "react",
            "end": "end",
        },
    )

    # safety_block → recommend / end
    graph.add_conditional_edges(
        "safety_block",
        route_after_safety,
        {
            "recommend": "recommend",
            "end": "end",
        },
    )

    # recommend → inventory（无条件的线性链）
    graph.add_edge("recommend", "inventory")

    # inventory → react / end
    graph.add_conditional_edges(
        "inventory",
        route_after_inventory,
        {
            "react": "react",
            "end": "end",
        },
    )

    # react → end
    graph.add_edge("react", "end")

    # end → END
    graph.add_edge("end", END)

    return graph.compile()


# ═══════════════════════════════════════════════════════════
# 节点工厂
# ═══════════════════════════════════════════════════════════


class _StateProxy:
    """工具 get_recommendation / get_user_profile 的 state 代理。

    graph build 时创建，每次 react_node 调用前由 react_node 更新。
    """

    def __init__(self):
        self.recommendations: list[dict] = []
        self.user_profile: dict = {}


def _make_react(
    llm_client: LLMClient,
    drug_repo_factory,
    retriever: DrugManualRetriever,
    react_profile: LLMProfile | None = None,
):
    """创建 react 节点的闭包。

    Skills 架构（v3）：
      - TaskClassifier (LLM #1): 语义分类 + 参数提取
      - SOPEngine (Code):       确定性工具链执行
      - ResponseGenerator (LLM #2): 结构化数据 → 自然语言
      - SkillRouter (Code):     intent → task_type 确定性路由
      - ReactAgent (fallback):  处理闲聊和未分类查询
    """
    state_proxy = _StateProxy()

    # ── 联网搜索服务（Tavily） ──
    web_search_service = TavilySearchService(Settings())

    # ── 工具列表（SOPEngine + ReactAgent fallback 共享） ──
    tools: list[BaseTool] = [
        SearchDrugTool(drug_repo_factory=drug_repo_factory),
        GetDrugDetailTool(drug_repo_factory=drug_repo_factory),
        SearchManualTool(retriever=retriever),
        SearchWebTool(web_search_service=web_search_service),
        GetRecommendationTool(state_proxy=state_proxy),
        GetUserProfileTool(state_proxy=state_proxy),
    ]

    # ── 注册工具 ──
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool.definition, tool.execute)

    # ── Skills 组件 ──
    skill_router = SkillRouter()
    sop_engine = SOPEngine(tool_registry=registry)

    # TaskClassifier: 低 temperature 保证分类一致性
    classifier_profile = LLMProfile(
        model=react_profile.model if react_profile else "qwen-plus",
        temperature=0.1,
        max_tokens=512,
    )
    task_classifier = TaskClassifier(
        llm_client=llm_client,
        profile=classifier_profile,
    )
    # ResponseGenerator: 独立 profile，max_tokens 更大
    # — 长回复（对比表格、安全提醒等）需要 2048 tokens
    # — 与 ReactAgent 的 1024 分离，避免截断
    generator_profile = LLMProfile(
        model=react_profile.model if react_profile else "qwen-plus",
        temperature=0.3,
        max_tokens=2048,
    )
    response_generator = ResponseGenerator(
        llm_client=llm_client,
        profile=generator_profile,
    )

    # ── 构建增强 system prompt（ReAct fallback 用） ──
    enhanced_prompt = REACT_SYSTEM_PROMPT + _build_tool_fallback_section(tools)

    # ── 创建 ReactAgent fallback ──
    react_agent = ReactAgent(
        llm_client=llm_client,
        system_prompt=enhanced_prompt,
        tool_registry=registry,
        profile=react_profile,
        max_iterations=5,
    )

    async def wrapped(state: ConversationState) -> dict:
        return await react_node(
            state=state,
            react_agent=react_agent,
            state_proxy=state_proxy,
            skill_router=skill_router,
            sop_engine=sop_engine,
            task_classifier=task_classifier,
            response_generator=response_generator,
        )

    return wrapped


def _make_recommend(llm_client, drug_repo_factory, weight_repo_factory,
                    retriever, scoring_pipeline, drug_graph_repo=None, vocab_source=None):
    async def wrapped(state: ConversationState) -> dict:
        async with drug_repo_factory() as drug_repo, weight_repo_factory() as weight_repo:
            return await recommend_node(
                state, llm_client=llm_client, drug_repo=drug_repo,
                weight_repo=weight_repo, retriever=retriever,
                scoring_pipeline=scoring_pipeline,
                drug_graph_repo=drug_graph_repo, vocab_source=vocab_source,
            )
    return wrapped


def _make_inventory(inventory_repo_factory, drug_repo_factory):
    async def wrapped(state: ConversationState) -> dict:
        async with inventory_repo_factory() as inv_repo, drug_repo_factory() as drug_repo:
            return await inventory_node(state, inventory_repo=inv_repo, drug_repo=drug_repo)
    return wrapped


def _make_end(session_repo_factory, safety_log_repo_factory):
    async def wrapped(state: ConversationState) -> dict:
        async with session_repo_factory() as session_repo, safety_log_repo_factory() as safety_log_repo:
            return await end_node(state, session_repo=session_repo, safety_log_repo=safety_log_repo)
    return wrapped


# ═══════════════════════════════════════════════════════════
# 容错矩阵自动生成
# ═══════════════════════════════════════════════════════════


def _build_tool_fallback_section(tools: list[BaseTool]) -> str:
    """从工具元数据自动生成容错策略段落，注入到 system prompt 末尾。

    遍历所有工具的 fallback_tools，生成工具替代关系表。
    这样 LLM 知道每个工具失败后可以尝试哪些替代工具。
    """
    lines = [
        "",
        "## 工具容错策略（重要）",
        "",
        '如果某个工具返回了 error（不是"未找到"，而是执行失败），'
        '不要立即放弃——可能有替代工具可以使用。请按以下策略尝试：',
        "",
    ]

    for tool in tools:
        if tool.fallback_tools:
            name = tool.definition.name
            fallbacks = "、".join(tool.fallback_tools)
            lines.append(f"- **{name} 失败** → 尝试 {fallbacks}")

    lines.append("")
    lines.append(
        '注意：get_recommendation 和 get_user_profile 没有替代工具'
        '——如果它们失败，跳过个性化部分，用通用方式回答。'
    )
    lines.append("")
    lines.append(
        '⚠️ **最终底线**：只有在所有相关替代工具都失败后，'
        '才能告知用户“抱歉，药品查询服务暂时不可用，'
        '建议您稍后再试或咨询药师”。'
        '在此之前，绝对不能凭训练数据编造药品信息。'
    )

    return "\n".join(lines)
