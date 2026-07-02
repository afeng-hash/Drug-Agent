"""Graph builder — assembles the LangGraph state machine."""

from functools import partial

from langgraph.graph import END, StateGraph

from app.db.repositories.drug import DrugRepository
from app.db.repositories.inventory import InventoryRepository
from app.db.repositories.safety_log import SafetyLogRepository
from app.db.repositories.session import SessionRepository
from app.graph.nodes.consult import consult_node
from app.graph.nodes.dispatcher import dispatcher_node
from app.graph.nodes.end import end_node
from app.graph.nodes.explain import explain_node
from app.graph.nodes.intake import intake_node
from app.graph.nodes.inventory import inventory_node
from app.graph.nodes.recommend import recommend_node
from app.graph.nodes.safety_check import safety_check_node
from app.graph.router import (
    route_after_consult,
    route_after_dispatcher,
    route_after_safety,
)
from app.graph.state import ConversationState
from app.llm.client import LLMClient
from app.rag.retriever import DrugManualRetriever
from app.rules.engine import RuleEngine


def build_graph(
    llm_client: LLMClient,
    rule_engine: RuleEngine,
    drug_repo_factory,
    inventory_repo_factory,
    session_repo_factory,
    safety_log_repo_factory,
    retriever: DrugManualRetriever,
    max_consult_rounds: int = 6,
) -> StateGraph:
    """Build and compile the LangGraph state machine.

    Args:
        llm_client: LLM client instance.
        rule_engine: Rule engine with registered rules.
        drug_repo_factory: Callable that returns DrugRepository (per-session DB).
        inventory_repo_factory: Callable that returns InventoryRepository.
        session_repo_factory: Callable that returns SessionRepository.
        safety_log_repo_factory: Callable that returns SafetyLogRepository.
        retriever: RAG retriever instance.
        max_consult_rounds: Max consult follow-up rounds.

    Returns:
        Compiled LangGraph StateGraph.
    """
    graph = StateGraph(ConversationState)

    # ── Add nodes ──
    # Nodes are async functions; LangGraph handles the async execution.
    # We use partial to inject dependencies via closure.
    graph.add_node("intake", intake_node)
    graph.add_node("dispatcher", partial(dispatcher_node, llm_client=llm_client))
    graph.add_node(
        "consult",
        partial(consult_node, llm_client=llm_client, max_rounds=max_consult_rounds),
    )
    graph.add_node(
        "safety_check",
        partial(safety_check_node, rule_engine=rule_engine),
    )
    # recommend, explain, inventory, end need DB sessions — we use factories
    graph.add_node(
        "recommend",
        _make_recommend(llm_client, drug_repo_factory),
    )
    graph.add_node(
        "explain",
        _make_explain(llm_client, drug_repo_factory, retriever),
    )
    graph.add_node(
        "inventory",
        _make_inventory(inventory_repo_factory, drug_repo_factory),
    )
    graph.add_node(
        "end",
        _make_end(session_repo_factory, safety_log_repo_factory),
    )

    # ── Add edges ──
    graph.set_entry_point("intake")
    graph.add_edge("intake", "dispatcher")

    # Dispatcher: conditional routing
    graph.add_conditional_edges(
        "dispatcher",
        route_after_dispatcher,
        {
            "consult": "consult",
            "explain": "explain",
            "recommend": "recommend",
            "end": "end",
        },
    )

    # Consult: if done → safety_check; otherwise → end (wait for user)
    graph.add_conditional_edges(
        "consult",
        route_after_consult,
        {
            "safety_check": "safety_check",
            "end": "end",
        },
    )

    # Safety check: BLOCK → end; PASS/FILTER → recommend
    graph.add_conditional_edges(
        "safety_check",
        route_after_safety,
        {
            "recommend": "recommend",
            "end": "end",
        },
    )

    # Linear tail: recommend → inventory → end
    graph.add_edge("recommend", "inventory")
    graph.add_edge("inventory", "end")

    # Explain and end are terminal (wait for next turn)
    graph.add_edge("explain", "end")
    graph.add_edge("end", END)

    return graph.compile()


# ── Node factory wrappers (closure injection of per-request DB sessions) ──

def _make_recommend(llm_client, drug_repo_factory):
    async def wrapped(state: ConversationState) -> dict:
        async with drug_repo_factory() as drug_repo:
            return await recommend_node(
                state, llm_client=llm_client, drug_repo=drug_repo
            )
    return wrapped


def _make_explain(llm_client, drug_repo_factory, retriever):
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
    async def wrapped(state: ConversationState) -> dict:
        async with inventory_repo_factory() as inv_repo, drug_repo_factory() as drug_repo:
            return await inventory_node(
                state,
                inventory_repo=inv_repo,
                drug_repo=drug_repo,
            )
    return wrapped


def _make_end(session_repo_factory, safety_log_repo_factory):
    async def wrapped(state: ConversationState) -> dict:
        async with session_repo_factory() as session_repo, safety_log_repo_factory() as safety_log_repo:
            return await end_node(
                state,
                session_repo=session_repo,
                safety_log_repo=safety_log_repo,
            )
    return wrapped
