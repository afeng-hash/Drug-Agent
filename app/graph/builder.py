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
from app.agent.react.schemas import ToolDefinition
from app.agent.react.tools import ToolRegistry
from app.graph.nodes.consult import consult_node
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

    工厂内完成：
      1. ToolRegistry + 5 个工具注册
      2. ReactAgent 实例化
      3. react_node 的 partial 绑定
    """
    state_proxy = _StateProxy()
    registry = ToolRegistry()

    # 工具 1: search_drug
    registry.register(
        ToolDefinition(
            name="search_drug",
            description="搜索药品。根据药品名称（通用名/商品名/拼音）模糊搜索，返回匹配的药品列表。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "药品名称关键词"},
                    "limit": {"type": "integer", "description": "返回数量上限，默认 5"},
                },
                "required": ["query"],
            },
        ),
        _make_search_drug(drug_repo_factory),
    )

    # 工具 2: get_drug_detail
    registry.register(
        ToolDefinition(
            name="get_drug_detail",
            description="获取药品完整信息：适应症、用法用量、不良反应、禁忌、相互作用等。",
            parameters={
                "type": "object",
                "properties": {
                    "drug_name": {"type": "string", "description": "药品通用名"},
                },
                "required": ["drug_name"],
            },
        ),
        _make_get_drug_detail(drug_repo_factory, retriever),
    )

    # 工具 3: search_manual
    registry.register(
        ToolDefinition(
            name="search_manual",
            description="在药品说明书中检索与用户问题相关的片段。",
            parameters={
                "type": "object",
                "properties": {
                    "drug_name": {"type": "string", "description": "药品名称"},
                    "question": {"type": "string", "description": "用户关心的问题"},
                    "top_k": {"type": "integer", "description": "返回片段数量，默认 5"},
                },
                "required": ["drug_name", "question"],
            },
        ),
        _make_search_manual(retriever),
    )

    # 工具 4: get_recommendation
    registry.register(
        ToolDefinition(
            name="get_recommendation",
            description="获取系统当前已推荐的药品列表。用于解析'这个药'等指代。",
            parameters={"type": "object", "properties": {}},
        ),
        _make_get_recommendation(state_proxy),
    )

    # 工具 5: get_user_profile
    registry.register(
        ToolDefinition(
            name="get_user_profile",
            description="获取用户个人信息（年龄、过敏史、慢性病、特殊人群等）。用于个性化回答。",
            parameters={"type": "object", "properties": {}},
        ),
        _make_get_user_profile(state_proxy),
    )

    react_agent = ReactAgent(
        llm_client=llm_client,
        system_prompt=REACT_SYSTEM_PROMPT,
        tool_registry=registry,
        profile=react_profile,
        max_iterations=5,
    )

    async def wrapped(state: ConversationState) -> dict:
        return await react_node(
            state=state,
            react_agent=react_agent,
            state_proxy=state_proxy,
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
# 工具 Executor 工厂
# ═══════════════════════════════════════════════════════════

def _make_search_drug(drug_repo_factory):
    async def execute(query: str, limit: int = 5):
        async with drug_repo_factory() as repo:
            results = await repo.search(query, limit=limit)
            return [
                {
                    "drug_id": getattr(r, "drug_id", r.get("drug_id", "")),
                    "generic_name": getattr(r, "generic_name", r.get("generic_name", "")),
                    "trade_names": getattr(r, "trade_names", r.get("trade_names", "")),
                }
                for r in results
            ]
    return execute


def _make_get_drug_detail(drug_repo_factory, retriever):
    async def execute(drug_name: str):
        async with drug_repo_factory() as repo:
            drug = await repo.get_by_name(drug_name)
            if not drug:
                return {"error": f"未找到药品：{drug_name}"}
            drug_dict = drug if isinstance(drug, dict) else {
                "drug_id": getattr(drug, "drug_id", None),
                "generic_name": getattr(drug, "generic_name", drug_name),
                "trade_names": getattr(drug, "trade_names", ""),
                "category": getattr(drug, "category", ""),
                "indications": getattr(drug, "indications", ""),
                "usage_dosage": getattr(drug, "usage_dosage", ""),
                "adverse_reactions": getattr(drug, "adverse_reactions", ""),
                "contraindications": getattr(drug, "contraindications", ""),
                "interactions": getattr(drug, "interactions", ""),
                "precautions": getattr(drug, "precautions", ""),
            }
            if retriever:
                try:
                    chunks = await retriever.retrieve_multi(
                        drug_name, question="适应症 不良反应 禁忌 用法用量", top_k=5
                    )
                    drug_dict["manual_chunks"] = [
                        c.content if hasattr(c, "content") else str(c) for c in chunks
                    ]
                except Exception:
                    pass
            return drug_dict
    return execute


def _make_search_manual(retriever):
    async def execute(drug_name: str, question: str, top_k: int = 5):
        if not retriever:
            return {"error": "说明书检索服务不可用"}
        try:
            chunks = await retriever.retrieve_multi(drug_name, question=question, top_k=top_k)
            return [
                {
                    "content": c.content if hasattr(c, "content") else str(c),
                    "source": getattr(c, "source", "") if hasattr(c, "source") else "",
                }
                for c in chunks
            ]
        except Exception as e:
            return {"error": f"检索失败：{str(e)}"}
    return execute


def _make_get_recommendation(state_proxy: _StateProxy):
    async def execute():
        return list(state_proxy.recommendations)
    return execute


def _make_get_user_profile(state_proxy: _StateProxy):
    async def execute():
        return dict(state_proxy.user_profile)
    return execute
