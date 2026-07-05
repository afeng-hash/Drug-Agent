"""
Graph builder — 组装 LangGraph 状态机。

这是整个系统的"大脑骨架"——定义节点、边、条件路由，把所有组件串联起来。

流程图：
  intake ──▶ dispatcher ──▶ consult ──▶ safety_block ──▶ recommend ──▶ inventory ──▶ end
                 │    │         │            │
                 │    │  (ask)→ end        (BLOCK)→ end
                 │    │
                 │    ├── explain ──▶ end
                 │    └── end

关键设计：recommend 永远是 consult→done 的自然结果，dispatcher 不能直达 recommend。
"""

from functools import partial

from langgraph.graph import END, StateGraph

from app.graph.nodes.consult import consult_node
from app.graph.nodes.dispatcher import dispatcher_node
from app.graph.nodes.end import end_node
from app.graph.nodes.explain import explain_node
from app.graph.nodes.intake import intake_node
from app.graph.nodes.inventory import inventory_node
from app.graph.nodes.recommend import recommend_node
from app.graph.nodes.safety_check import safety_block_node
from app.graph.router import (
    route_after_consult,
    route_after_dispatcher,
    route_after_safety,
)
from app.graph.state import ConversationState
from app.llm.client import LLMClient
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
    max_consult_rounds: int = 6,
) -> StateGraph:
    """构建并编译 LangGraph 状态机。

    在 FastAPI lifespan 的 startup 阶段调用一次。
    返回的 compiled graph 挂到 app.state.graph 上，每次请求时复用。

    Args:
        llm_client:          LLM 客户端（通义千问）
        rule_engine:         安全规则引擎（已注册所有规则）
        drug_repo_factory:   药品仓库工厂（每次调用创建新 DB 会话）
        inventory_repo_factory: 库存仓库工厂
        session_repo_factory:   会话仓库工厂
        safety_log_repo_factory: 安全日志仓库工厂
        retriever:           Milvus 药品说明书检索器
        max_consult_rounds:  问诊最多追问轮数，超过此数强制进入推荐

    Returns:
        编译好的 LangGraph 图（可直接调用 .astream_events()）
    """
    graph = StateGraph(ConversationState)

    # ── 添加节点 ──────────────────────────────────────────
    # 每个节点是一个 async 函数，接收 state，返回部分更新的 dict
    # 用 partial 把不可序列化的依赖（LLM client 等）通过闭包注入

    graph.add_node("intake", intake_node)
    """预处理节点：更新 phase，不做复杂逻辑"""

    graph.add_node("dispatcher", partial(dispatcher_node, llm_client=llm_client))
    """路由分发节点：LLM 分析用户意图，决定下一步走哪个节点"""

    graph.add_node(
        "consult",
        partial(consult_node, llm_client=llm_client, max_rounds=max_consult_rounds),
    )
    """症状问诊节点：多轮追问，收集症状槽位"""

    graph.add_node(
        "safety_block",
        partial(safety_block_node, rule_engine=rule_engine),
    )
    """安全拦截节点：仅症状级 BLOCK（高烧/婴儿/孕妇/紧急），不含药品级禁忌"""

    # recommend / explain / inventory / end 需要每次新开 DB 会话，
    # 所以用工厂模式（_make_* 闭包），而不是直接传 repo 实例
    graph.add_node(
        "recommend",
        _make_recommend(llm_client, drug_repo_factory, weight_repo_factory, retriever, scoring_pipeline, drug_graph_repo, vocab_source),
    )
    """药品推荐节点：ScoringPipeline 排序 + RAG 说明书 + LLM 文案"""

    graph.add_node(
        "explain",
        _make_explain(llm_client, drug_repo_factory, retriever),
    )
    """药品解释节点：DB + RAG → 格式化的药品说明书"""

    graph.add_node(
        "inventory",
        _make_inventory(inventory_repo_factory, drug_repo_factory),
    )
    """库存节点：查询推荐药品的库存和价格"""

    graph.add_node(
        "end",
        _make_end(session_repo_factory, safety_log_repo_factory),
    )
    """结束节点：持久化消息、记录安全日志"""

    # ── 添加边 ──────────────────────────────────────────

    # 入口 → intake
    graph.set_entry_point("intake")

    # intake → dispatcher（无条件）
    graph.add_edge("intake", "dispatcher")

    # dispatcher → 3 路条件分支
    # （recommend 路由已移除——推荐永远是 consult→done 的自然结果）
    graph.add_conditional_edges(
        "dispatcher",
        route_after_dispatcher,           # 条件函数：读 dispatcher_result.route
        {
            "consult": "consult",          # 症状相关（描述、回答、个人信息、推荐意愿、换药）
            "explain": "explain",          # 药品咨询
            "end": "end",                  # 结束
        },
    )

    # consult → 2 路条件分支
    graph.add_conditional_edges(
        "consult",
        route_after_consult,              # 条件函数：读 consult_next_action
        {
            "safety_block": "safety_block",  # done → 进入安全拦截
            "end": "end",                    # ask → 等待用户下一轮输入
        },
    )

    # safety_block → 2 路条件分支
    graph.add_conditional_edges(
        "safety_block",
        route_after_safety,               # 条件函数：读 safety_result.verdict
        {
            "recommend": "recommend",      # PASS → 继续推荐
            "end": "end",                  # BLOCK → 终止，返回警告
        },
    )

    # 推荐链（线性）：recommend → inventory → end
    graph.add_edge("recommend", "inventory")
    graph.add_edge("inventory", "end")

    # explain → end（解释完就结束本轮）
    graph.add_edge("explain", "end")

    # end → 图结束（触发 .astream_events() 停止）
    graph.add_edge("end", END)

    # ── 编译 ──
    return graph.compile()


# ──────────────────────────────────────────────────────────
# 节点工厂函数（闭包注入 DB 仓库）
#
# 为什么需要工厂？
#   LangGraph 的节点函数签名是 (state) → dict。但 recommend/explain/
#   inventory/end 节点需要 DB session。如果直接把 repo 传给节点，
#   repo 绑定的 session 会在整个应用生命周期内一直存在（连接池泄露）。
#
#   factory 模式：每次节点被调用时，通过工厂函数新开一个 DB 会话，
#   用完即关。这样每个 Graph run 都有独立的短生命周期 DB session。
# ──────────────────────────────────────────────────────────


def _make_recommend(llm_client, drug_repo_factory, weight_repo_factory, retriever, scoring_pipeline, drug_graph_repo=None, vocab_source=None):
    """创建 recommend 节点的闭包。

    调用时机：每次 Graph 运行到 recommend 节点时。
    内部逻辑：Neo4j 图查询药品 → ScoringPipeline 排序 → RAG 查说明书 → LLM 写推荐文案 → 返回推荐。
    """
    async def wrapped(state: ConversationState) -> dict:
        async with drug_repo_factory() as drug_repo, weight_repo_factory() as weight_repo:
            return await recommend_node(
                state,
                llm_client=llm_client,
                drug_repo=drug_repo,
                weight_repo=weight_repo,
                retriever=retriever,
                scoring_pipeline=scoring_pipeline,
                drug_graph_repo=drug_graph_repo,
                vocab_source=vocab_source,
            )
    return wrapped


def _make_explain(llm_client, drug_repo_factory, retriever):
    """创建 explain 节点的闭包。

    调用时机：Dispatcher 判定用户在问某个药品时。
    内部逻辑：DB 查药品基本信息 + Milvus 向量检索说明书 → LLM 格式化解释。
    """
    async def wrapped(state: ConversationState) -> dict:
        async with drug_repo_factory() as drug_repo:
            return await explain_node(
                state,
                llm_client=llm_client,
                drug_repo=drug_repo,
                retriever=retriever,
            )
    return wrapped


def _make_inventory(inventory_repo_factory, drug_repo_factory):
    """创建 inventory 节点的闭包。

    调用时机：recommend 节点之后。
    内部逻辑：根据推荐药品的 drug_id 查库存表 → 格式化库存信息 → 追加到 response。
    """
    async def wrapped(state: ConversationState) -> dict:
        async with inventory_repo_factory() as inv_repo, drug_repo_factory() as drug_repo:
            return await inventory_node(
                state,
                inventory_repo=inv_repo,
                drug_repo=drug_repo,
            )
    return wrapped


def _make_end(session_repo_factory, safety_log_repo_factory):
    """创建 end 节点的闭包。

    调用时机：每个 turn 的最后一步。
    内部逻辑：保存 AI 回复到 messages 表 + 记录安全日志到 safety_logs 表。
    """
    async def wrapped(state: ConversationState) -> dict:
        async with session_repo_factory() as session_repo, safety_log_repo_factory() as safety_log_repo:
            return await end_node(
                state,
                session_repo=session_repo,
                safety_log_repo=safety_log_repo,
            )
    return wrapped
