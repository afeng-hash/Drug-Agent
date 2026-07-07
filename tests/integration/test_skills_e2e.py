"""
端到端集成测试 — Skills 管线（真实 LLM + 真实 DB + 真实 Milvus + 真实 Tavily）。

运行方式：
  pytest tests/integration/test_skills_e2e.py -v -s --log-cli-level=INFO

需要基础设施：
  - PostgreSQL (docker-compose)
  - Milvus (docker-compose)
  - Neo4j (本地)
  - DashScope API (LLM)
  - Tavily API (联网搜索)

每个测试用例打印完整链路日志，用于分析分类准确性、回复质量、降级行为。
"""

import asyncio
import logging
import sys
import time
from pathlib import Path

import pytest
import pytest_asyncio

# 确保项目根目录在 path 中
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.agent.prompts import REACT_SYSTEM_PROMPT
from app.agent.react.agent import ReactAgent
from app.agent.react.skills import (
    SOPEngine,
    SkillRouter,
    TaskClassifier,
    ResponseGenerator,
    TASK_SOP_MAP,
)
from app.agent.react.skills.types import TaskType
from app.agent.react.tools import ToolRegistry
from app.agent.react.tools.get_drug_detail import GetDrugDetailTool
from app.agent.react.tools.get_recommendation import GetRecommendationTool
from app.agent.react.tools.get_user_profile import GetUserProfileTool
from app.agent.react.tools.search_drug import SearchDrugTool
from app.agent.react.tools.search_manual import SearchManualTool
from app.agent.react.tools.search_web import SearchWebTool
from app.config import Settings
from app.db.database import close_db, get_db, init_db
from app.db.repositories.drug import DrugRepository
from app.graph.builder import _StateProxy, _build_tool_fallback_section
from app.graph.nodes.react import _build_classify_context, _build_sop_params, _execute, _run_react_fallback
from app.llm.client import LLMClient
from app.llm.profile import LLMProfile
from app.rag.retriever import DrugManualRetriever
from app.search.service import TavilySearchService

# ── 日志 ─────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.WARNING,  # 抑制第三方库的 DEBUG 日志
    format="%(name)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("e2e_test")
logger.setLevel(logging.DEBUG)

# 关键模块打开 INFO
for name in ("app.agent.react.skills", "app.graph.nodes.react"):
    logging.getLogger(name).setLevel(logging.DEBUG)


# ═══════════════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════════════

def _sep(title: str) -> str:
    """分隔线"""
    return f"\n{'═' * 60}\n  {title}\n{'═' * 60}"


def _sub(title: str) -> str:
    """子分隔线"""
    return f"{'─' * 50}\n  {title}"


# ═══════════════════════════════════════════════════════════════
# Fixtures（module 级别，复用昂贵资源）
# ═══════════════════════════════════════════════════════════════


@pytest_asyncio.fixture(scope="module")
async def settings():
    """加载 .env 配置"""
    s = Settings()
    print(_sep("Settings 加载完成"))
    print(f"  LLM: {s.llm_model} @ {s.llm_base_url}")
    print(f"  DB: {s.database_url}")
    print(f"  Milvus: {s.milvus_host}:{s.milvus_port}")
    print(f"  Tavily: {'已配置' if s.tavily_api_key else '未配置'}")
    return s


@pytest_asyncio.fixture(scope="module")
async def llm_client(settings):
    """真实 LLM 客户端（DashScope）"""
    client = LLMClient(settings)
    print(_sub("LLMClient 创建完成"))
    return client


@pytest_asyncio.fixture(scope="module")
async def db_initialized(settings):
    """初始化数据库连接池"""
    await init_db(settings)
    print(_sub("数据库初始化完成"))
    yield
    await close_db()
    print(_sub("数据库连接已释放"))


@pytest_asyncio.fixture(scope="module")
async def retriever(settings, llm_client):
    """Milvus 检索器"""
    r = DrugManualRetriever(settings, llm_client)
    try:
        await r.ensure_collection()
        print(_sub("Milvus 检索器就绪"))
    except Exception as e:
        print(f"  ⚠ Milvus 连接异常: {e}")
    return r


@pytest_asyncio.fixture(scope="module")
async def web_search_service(settings):
    """Tavily 联网搜索服务"""
    svc = TavilySearchService(settings)
    print(_sub(f"TavilySearchService 就绪 (available={svc.is_available})"))
    return svc


@pytest_asyncio.fixture(scope="module")
async def tool_registry(db_initialized, retriever, web_search_service):
    """完整工具注册中心（与 builder.py _make_react 一致）"""
    # repo 工厂（模拟 main.py 的 _repo_context）
    def drug_repo_factory():
        async def _factory():
            db_ctx = get_db()
            db = await db_ctx.__aenter__()
            repo = DrugRepository(db)
            return repo, db_ctx, db
        return _factory()

    # 简化的工厂包装
    class _RepoFactory:
        def __call__(self):
            return _DrugRepoContext()

    class _DrugRepoContext:
        async def __aenter__(self):
            self._ctx = get_db()
            self._db = await self._ctx.__aenter__()
            return DrugRepository(self._db)

        async def __aexit__(self, *args):
            await self._ctx.__aexit__(*args)

    factory = _RepoFactory()

    state_proxy = _StateProxy()

    tools = [
        SearchDrugTool(drug_repo_factory=factory),
        GetDrugDetailTool(drug_repo_factory=factory),
        SearchManualTool(retriever=retriever),
        SearchWebTool(web_search_service=web_search_service),
        GetRecommendationTool(state_proxy=state_proxy),
        GetUserProfileTool(state_proxy=state_proxy),
    ]

    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool.definition, tool.execute)

    print(_sub(f"ToolRegistry 就绪: {registry.tool_names}"))
    return registry


@pytest_asyncio.fixture(scope="module")
async def react_profile():
    """ReactAgent LLMProfile"""
    return LLMProfile(model="qwen-plus", temperature=0.3, max_tokens=1024)


@pytest_asyncio.fixture(scope="module")
async def skills_components(llm_client, tool_registry, react_profile):
    """创建所有 Skills 组件 + ReactAgent fallback（与 builder.py _make_react 一致）"""
    skill_router = SkillRouter()
    sop_engine = SOPEngine(tool_registry=tool_registry)

    classifier_profile = LLMProfile(
        model=react_profile.model,
        temperature=0.1,
        max_tokens=512,
    )
    task_classifier = TaskClassifier(
        llm_client=llm_client,
        profile=classifier_profile,
    )
    response_generator = ResponseGenerator(
        llm_client=llm_client,
        profile=react_profile,
    )

    # ReactAgent fallback
    from app.agent.react.tools.base import BaseTool
    tools_for_fallback = [
        SearchDrugTool(drug_repo_factory=lambda: _drug_repo_ctx()),
        GetDrugDetailTool(drug_repo_factory=lambda: _drug_repo_ctx()),
        SearchManualTool(retriever=None),  # fallback 时不依赖 retriever
        SearchWebTool(web_search_service=None),
    ]
    # 简化：复用 tool_registry 的工具执行器
    enhanced_prompt = REACT_SYSTEM_PROMPT
    react_agent = ReactAgent(
        llm_client=llm_client,
        system_prompt=enhanced_prompt,
        tool_registry=tool_registry,
        profile=react_profile,
        max_iterations=5,
    )

    print(_sub("Skills 组件创建完成"))
    print(f"  SkillRouter: [OK]")
    print(f"  SOPEngine: [OK]")
    print(f"  TaskClassifier: model={classifier_profile.model}, temp={classifier_profile.temperature}")
    print(f"  ResponseGenerator: model={react_profile.model}, temp={react_profile.temperature}")
    print(f"  ReactAgent (fallback): max_iterations=5")

    return {
        "skill_router": skill_router,
        "sop_engine": sop_engine,
        "task_classifier": task_classifier,
        "response_generator": response_generator,
        "react_agent": react_agent,
    }


def _drug_repo_ctx():
    """简化版 DrugRepository 上下文管理器（用于工具执行）"""
    from app.db.database import get_db as _get_db
    from app.db.repositories.drug import DrugRepository as _DrugRepo

    class _Ctx:
        async def __aenter__(self):
            self._ctx = _get_db()
            self._db = await self._ctx.__aenter__()
            return _DrugRepo(self._db)
        async def __aexit__(self, *args):
            await self._ctx.__aexit__(*args)
    return _Ctx()


# ═══════════════════════════════════════════════════════════════
# 测试用例
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@pytest.mark.integration
class TestSkillsPipelineE2E:
    """完整 SOP 管线端到端测试

    每条测试打印：
      - TaskClassifier 的分类结果
      - SOPEngine 每步执行结果
      - ResponseGenerator 的最终输出
    """

    # ── 1. 副作用 ──────────────────────────────────────────

    async def test_01_side_effects(self, skills_components):
        """副作用查询：布洛芬有什么副作用"""
        c = skills_components
        query = "布洛芬有什么副作用"
        intent = "ask_drug"
        history = [{"role": "user", "content": query}]

        print(_sep(f"测试: {query}"))

        # ── Step 1: SkillRouter ──
        task_type = c["skill_router"].route(intent=intent, query=query)
        classification = None
        print(f"[SkillRouter] intent={intent} → {task_type}")

        # ── Step 2: TaskClassifier ──
        if task_type is None:
            t0 = time.time()
            classification = await c["task_classifier"].classify(
                query=query,
                history=history,
                context={},
            )
            elapsed = time.time() - t0
            task_type = classification.task_type
            print(f"[TaskClassifier] ({elapsed:.1f}s)")
            print(f"  task_type:   {classification.task_type}")
            print(f"  drug_names:  {classification.drug_names}")
            print(f"  confidence:  {classification.confidence}")
            print(f"  population:  {classification.population}")
            print(f"  custom_focus: {classification.custom_focus}")

        # ── Step 3: SOPEngine ──
        sop = TASK_SOP_MAP.get(task_type)
        assert sop is not None, f"未找到 SOP: {task_type}"

        sop_params = _build_sop_params(
            query=query,
            task_type=task_type,
            classification=classification,
            recommendations=[],
        )
        print(f"[_build_sop_params] {sop_params}")

        if sop_params:
            t0 = time.time()
            sop_result = await c["sop_engine"].execute(sop, sop_params)
            elapsed = time.time() - t0
            print(f"[SOPEngine] ({elapsed:.1f}s)")
            print(f"  task_type:         {sop_result.task_type}")
            print(f"  has_usable_data:   {sop_result.has_usable_data}")
            print(f"  triggered_web:     {sop_result.triggered_web_fallback}")
            for step in sop_result.steps:
                data_len = len(str(step.data)) if step.data else 0
                print(f"  Step {step.step_order} [{step.tool_name}]: success={step.success}, data_len={data_len}")
                if not step.success:
                    print(f"    error: {step.error}")

            # ── Step 4: ResponseGenerator ──
            t0 = time.time()
            response = await c["response_generator"].generate(
                query=query,
                sop_result=sop_result,
                sop=sop,
            )
            elapsed = time.time() - t0
            print(f"[ResponseGenerator] ({elapsed:.1f}s)")
            print(f"[最终回复] ({len(response)} chars)")
            print(f"{'─' * 50}")
            print(response[:800])
            if len(response) > 800:
                print(f"... (截断, 共 {len(response)} 字符)")
            print(f"{'─' * 50}")

            # 基本断言
            assert len(response) > 20, "回复过短"
        else:
            print("[SKIP] _build_sop_params 返回 None")
            pytest.skip("参数构建失败")

    # ── 2. 禁忌 ────────────────────────────────────────────

    async def test_02_contraindications(self, skills_components):
        """禁忌查询：有胃溃疡能吃布洛芬吗"""
        c = skills_components
        query = "有胃溃疡能吃布洛芬吗"
        intent = "ask_drug"

        print(_sep(f"测试: {query}"))

        task_type = c["skill_router"].route(intent=intent, query=query)
        classification = None
        print(f"[SkillRouter] intent={intent} → {task_type}")

        if task_type is None:
            t0 = time.time()
            classification = await c["task_classifier"].classify(
                query=query,
                history=[{"role": "user", "content": query}],
                context={},
            )
            print(f"[TaskClassifier] ({time.time()-t0:.1f}s)")
            print(f"  task_type={classification.task_type}, drugs={classification.drug_names}, conf={classification.confidence}")
            task_type = classification.task_type

        sop = TASK_SOP_MAP.get(task_type)
        assert sop is not None

        sop_params = _build_sop_params(query, task_type, classification, [])
        print(f"[_build_sop_params] {sop_params}")

        if sop_params:
            sop_result = await c["sop_engine"].execute(sop, sop_params)
            response = await c["response_generator"].generate(query, sop_result, sop)
            print(f"[SOPEngine] has_data={sop_result.has_usable_data}, web={sop_result.triggered_web_fallback}")
            print(f"[最终回复] ({len(response)} chars)")
            print(f"{'─' * 50}")
            print(response[:600])
            if len(response) > 600:
                print(f"... (截断, 共 {len(response)} 字符)")
            print(f"{'─' * 50}")
            assert len(response) > 20
        else:
            pytest.skip("参数构建失败")

    # ── 3. 用法用量 ────────────────────────────────────────

    async def test_03_dosage(self, skills_components):
        """用法用量：布洛芬怎么吃"""
        c = skills_components
        query = "布洛芬怎么吃"
        intent = "ask_drug"

        print(_sep(f"测试: {query}"))

        task_type = c["skill_router"].route(intent=intent, query=query)
        classification = None
        print(f"[SkillRouter] intent={intent} → {task_type}")

        if task_type is None:
            classification = await c["task_classifier"].classify(
                query=query,
                history=[{"role": "user", "content": query}],
                context={},
            )
            print(f"[TaskClassifier] task_type={classification.task_type}, drugs={classification.drug_names}, conf={classification.confidence}")
            task_type = classification.task_type

        sop = TASK_SOP_MAP.get(task_type)
        assert sop is not None

        sop_params = _build_sop_params(query, task_type, classification, [])
        print(f"[_build_sop_params] {sop_params}")

        if sop_params:
            sop_result = await c["sop_engine"].execute(sop, sop_params)
            response = await c["response_generator"].generate(query, sop_result, sop)
            print(f"[SOPEngine] has_data={sop_result.has_usable_data}, web={sop_result.triggered_web_fallback}")
            print(f"[最终回复] ({len(response)} chars)")
            print(f"{'─' * 50}")
            print(response[:600])
            if len(response) > 600:
                print(f"... (截断, 共 {len(response)} 字符)")
            print(f"{'─' * 50}")
            assert len(response) > 20
        else:
            pytest.skip("参数构建失败")

    # ── 4. 功效 ────────────────────────────────────────────

    async def test_04_efficacy(self, skills_components):
        """功效查询：布洛芬有什么作用"""
        c = skills_components
        query = "布洛芬有什么作用"
        intent = "ask_drug"

        print(_sep(f"测试: {query}"))

        task_type = c["skill_router"].route(intent=intent, query=query)
        classification = None
        print(f"[SkillRouter] intent={intent} → {task_type}")

        if task_type is None:
            classification = await c["task_classifier"].classify(
                query=query,
                history=[{"role": "user", "content": query}],
                context={},
            )
            print(f"[TaskClassifier] task_type={classification.task_type}, drugs={classification.drug_names}, conf={classification.confidence}")
            task_type = classification.task_type

        sop = TASK_SOP_MAP.get(task_type)
        assert sop is not None

        sop_params = _build_sop_params(query, task_type, classification, [])
        print(f"[_build_sop_params] {sop_params}")

        if sop_params:
            sop_result = await c["sop_engine"].execute(sop, sop_params)
            response = await c["response_generator"].generate(query, sop_result, sop)
            print(f"[SOPEngine] has_data={sop_result.has_usable_data}, web={sop_result.triggered_web_fallback}")
            print(f"[最终回复] ({len(response)} chars)")
            print(f"{'─' * 50}")
            print(response[:600])
            if len(response) > 600:
                print(f"... (截断, 共 {len(response)} 字符)")
            print(f"{'─' * 50}")
            assert len(response) > 20
        else:
            pytest.skip("参数构建失败")

    # ── 5. 特殊人群 ────────────────────────────────────────

    async def test_05_special_population(self, skills_components):
        """特殊人群：孕妇能吃布洛芬吗"""
        c = skills_components
        query = "孕妇能吃布洛芬吗"
        intent = "ask_drug"

        print(_sep(f"测试: {query}"))

        task_type = c["skill_router"].route(intent=intent, query=query)
        classification = None
        print(f"[SkillRouter] intent={intent} → {task_type}")

        if task_type is None:
            t0 = time.time()
            classification = await c["task_classifier"].classify(
                query=query,
                history=[{"role": "user", "content": query}],
                context={},
            )
            print(f"[TaskClassifier] ({time.time()-t0:.1f}s)")
            print(f"  task_type={classification.task_type}, drugs={classification.drug_names}")
            print(f"  population={classification.population}, conf={classification.confidence}")
            task_type = classification.task_type

        sop = TASK_SOP_MAP.get(task_type)
        assert sop is not None
        print(f"[SOP] task_type={task_type}, response_structure 前50字: {sop.response_structure[:50]}...")

        sop_params = _build_sop_params(query, task_type, classification, [])
        print(f"[_build_sop_params] {sop_params}")

        if sop_params:
            sop_result = await c["sop_engine"].execute(sop, sop_params)
            response = await c["response_generator"].generate(query, sop_result, sop)
            print(f"[SOPEngine] has_data={sop_result.has_usable_data}, web={sop_result.triggered_web_fallback}")
            print(f"[最终回复] ({len(response)} chars)")
            print(f"{'─' * 50}")
            print(response[:700])
            if len(response) > 700:
                print(f"... (截断, 共 {len(response)} 字符)")
            print(f"{'─' * 50}")
            assert len(response) > 20
        else:
            pytest.skip("参数构建失败")

    # ── 6. 药物相互作用（SkillRouter 直路由） ─────────────

    async def test_06_drug_interaction(self, skills_components):
        """药物相互作用：布洛芬和头孢能一起吃吗（Router 直路由）"""
        c = skills_components
        query = "布洛芬和头孢能一起吃吗"
        intent = "ask_interaction"

        print(_sep(f"测试: {query} (Router 直路由)"))

        # SkillRouter 应该直接识别
        task_type = c["skill_router"].route(intent=intent, query=query)
        classification = None
        print(f"[SkillRouter] intent={intent} → {task_type}")
        assert task_type == TaskType.DRUG_INTERACTION, f"Router 应识別为 DRUG_INTERACTION，实际: {task_type}"

        # 但仍需要 drug_names → 走 TaskClassifier 提取参数
        if not classification:
            classification = await c["task_classifier"].classify(
                query=query,
                history=[{"role": "user", "content": query}],
                context={},
            )
            print(f"[TaskClassifier] task_type={classification.task_type}, drugs={classification.drug_names}, conf={classification.confidence}")

        sop = TASK_SOP_MAP[task_type]
        sop_params = _build_sop_params(query, task_type, classification, [])
        print(f"[_build_sop_params] {sop_params}")

        if sop_params:
            t0 = time.time()
            sop_result = await c["sop_engine"].execute(sop, sop_params)
            print(f"[SOPEngine] ({time.time()-t0:.1f}s) has_data={sop_result.has_usable_data}, web={sop_result.triggered_web_fallback}")
            for step in sop_result.steps:
                data_len = len(str(step.data)) if step.data else 0
                print(f"  Step {step.step_order} [{step.tool_name}]: success={step.success}, data_len={data_len}")

            response = await c["response_generator"].generate(query, sop_result, sop)
            print(f"[最终回复] ({len(response)} chars)")
            print(f"{'─' * 50}")
            print(response[:700])
            if len(response) > 700:
                print(f"... (截断, 共 {len(response)} 字符)")
            print(f"{'─' * 50}")
            assert len(response) > 20
        else:
            # 可能是 drug_names 不够 2 个 → 走 fallback
            print("[WARN] _build_sop_params 返回 None，走 ReAct fallback")
            response = await _run_react_fallback(query, [{"role":"user","content":query}], None, c["react_agent"])
            print(f"[ReAct fallback] ({len(response)} chars)")
            print(response[:500])
            assert len(response) > 10

    # ── 7. 药品对比（SkillRouter 直路由） ──────────────────

    async def test_07_drug_comparison(self, skills_components):
        """药品对比：布洛芬和对乙酰氨基酚哪个好（Router 直路由）"""
        c = skills_components
        query = "布洛芬和对乙酰氨基酚哪个好"
        intent = "compare_drugs"

        print(_sep(f"测试: {query} (Router 直路由)"))

        task_type = c["skill_router"].route(intent=intent, query=query)
        classification = None
        print(f"[SkillRouter] intent={intent} → {task_type}")
        assert task_type == TaskType.DRUG_COMPARISON, f"Router 应识別为 DRUG_COMPARISON，实际: {task_type}"

        classification = await c["task_classifier"].classify(
            query=query,
            history=[{"role": "user", "content": query}],
            context={},
        )
        print(f"[TaskClassifier] task_type={classification.task_type}, drugs={classification.drug_names}, conf={classification.confidence}")

        sop = TASK_SOP_MAP[task_type]
        sop_params = _build_sop_params(query, task_type, classification, [])
        print(f"[_build_sop_params] {sop_params}")

        if sop_params:
            t0 = time.time()
            sop_result = await c["sop_engine"].execute(sop, sop_params)
            print(f"[SOPEngine] ({time.time()-t0:.1f}s) has_data={sop_result.has_usable_data}, web={sop_result.triggered_web_fallback}")
            for step in sop_result.steps:
                data_len = len(str(step.data)) if step.data else 0
                print(f"  Step {step.step_order} [{step.tool_name}]: success={step.success}, data_len={data_len}")

            response = await c["response_generator"].generate(query, sop_result, sop)
            print(f"[最终回复] ({len(response)} chars)")
            print(f"{'─' * 50}")
            print(response[:800])
            if len(response) > 800:
                print(f"... (截断, 共 {len(response)} 字符)")
            print(f"{'─' * 50}")
            assert len(response) > 20
        else:
            print("[WARN] _build_sop_params 返回 None，走 ReAct fallback")
            response = await _run_react_fallback(query, [{"role":"user","content":query}], None, c["react_agent"])
            print(f"[ReAct fallback] ({len(response)} chars)")
            print(response[:500])
            assert len(response) > 10

    # ── 8. 闲聊 → ReAct fallback ───────────────────────────

    async def test_08_chat_fallback(self, skills_components):
        """闲聊：你好 → ReAct fallback"""
        c = skills_components
        query = "你好"
        intent = "chat"

        print(_sep(f"测试: {query} (应走 ReAct fallback)"))

        t0 = time.time()
        response = await _run_react_fallback(
            query=query,
            history=[{"role": "user", "content": query}],
            workflow_context=None,
            react_agent=c["react_agent"],
        )
        elapsed = time.time() - t0
        print(f"[ReAct fallback] ({elapsed:.1f}s)")
        print(f"[最终回复] ({len(response)} chars)")
        print(f"{'─' * 50}")
        print(response[:400])
        print(f"{'─' * 50}")
        assert len(response) > 5, "闲聊 fallback 应有回复"

    # ── 9. 低置信度 → ReAct fallback ───────────────────────

    async def test_09_low_confidence_fallback(self, skills_components):
        """模糊查询 → 低置信度 → ReAct fallback"""
        c = skills_components
        query = "怎么样"
        intent = "ask_drug"

        print(_sep(f"测试: '{query}' (模糊查询，应走 ReAct fallback)"))

        classification = await c["task_classifier"].classify(
            query=query,
            history=[{"role": "user", "content": query}],
            context={},
        )
        print(f"[TaskClassifier] task_type={classification.task_type}, drugs={classification.drug_names}, conf={classification.confidence}")
        print(f"  MIN_CONFIDENCE={TaskClassifier.MIN_CONFIDENCE}, 低于阈值={classification.confidence < TaskClassifier.MIN_CONFIDENCE}")

        if classification.confidence < TaskClassifier.MIN_CONFIDENCE:
            t0 = time.time()
            response = await _run_react_fallback(
                query=query,
                history=[{"role": "user", "content": query}],
                workflow_context=None,
                react_agent=c["react_agent"],
            )
            print(f"[ReAct fallback] ({time.time()-t0:.1f}s)")
            print(f"[最终回复] ({len(response)} chars)")
            print(response[:300])
            assert len(response) > 5, "低置信度应走 ReAct fallback"
        else:
            print(f"[NOTE] 置信度 {classification.confidence} >= {TaskClassifier.MIN_CONFIDENCE}，未触发 fallback")
            pytest.skip("置信度未低于阈值，跳过")

    # ── 10. 数据不足兜底 ───────────────────────────────────

    async def test_10_no_data_fallback(self, skills_components):
        """不存在的药 → 数据为空 → 兜底模板"""
        c = skills_components
        query = "XYZ123这个药有什么副作用"
        intent = "ask_drug"

        print(_sep(f"测试: {query} (不存在的药品)"))

        classification = await c["task_classifier"].classify(
            query=query,
            history=[{"role": "user", "content": query}],
            context={},
        )
        print(f"[TaskClassifier] task_type={classification.task_type}, drugs={classification.drug_names}, conf={classification.confidence}")

        # 注意：LLM 可能提取 "XYZ123" 为 drug_name
        sop = TASK_SOP_MAP.get(TaskType.SIDE_EFFECTS, TASK_SOP_MAP[TaskType.EFFICACY])
        sop_params = _build_sop_params(query, TaskType.SIDE_EFFECTS, classification, [])
        print(f"[_build_sop_params] {sop_params}")

        if sop_params:
            sop_result = await c["sop_engine"].execute(sop, sop_params)
            print(f"[SOPEngine] has_data={sop_result.has_usable_data}, web={sop_result.triggered_web_fallback}")
            for step in sop_result.steps:
                data_len = len(str(step.data)) if step.data else 0
                print(f"  Step {step.step_order} [{step.tool_name}]: success={step.success}, data_len={data_len}")

            response = await c["response_generator"].generate(query, sop_result, sop)
            print(f"[最终回复] ({len(response)} chars)")
            print(f"{'─' * 50}")
            print(response[:600])
            print(f"{'─' * 50}")

            # 数据为空时应使用 fallback_response 或明确告知用户
            assert len(response) > 10
        else:
            pytest.skip("参数构建失败")


# ═══════════════════════════════════════════════════════════════
# 独立运行入口
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s", "--log-cli-level=INFO"])
