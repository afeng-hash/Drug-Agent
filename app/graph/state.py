"""
ConversationState — LangGraph 共享状态对象。

这是贯穿整个对话流程的"黑板"——每个 Graph 节点（Intake / Dispatcher / Consult /
SafetyCheck / Recommend / Explain / Inventory / End）都会读写这个对象。

它会在多轮对话之间持久化：同一 session 的不同 turn（用户每次发消息）共用同一个 state。
"""

from typing import Annotated, Any, TypedDict

from langgraph.graph.message import add_messages


class ConversationState(TypedDict):
    """对话全生命周期的共享状态。

    被 LangGraph 管理，所有节点函数接收 state，返回部分更新的 dict。
    LangGraph 会自动将返回的 dict 合并回 state（每个字段有独立 reducer）。

    ⚠️ 字段命名规范：
      - 所有字段必须是 Python 合法标识符（下划线分隔）
      - LangGraph StateGraph 限制：字段名不能包含 '.'（如 "node.events"）
      - 因此所有复合名称都用下划线：node_events, consult_slots 等
    """

    # ────────────────────────────────────────────
    # 基础标识
    # ────────────────────────────────────────────

    session_id: str
    """【会话ID】UUID v4 字符串，如 "f45c6a16-9754-4306-99c7-a537595160fb"。

    用途：
      - 前端通过它标识一次对话，每次请求都要带上
      - 后端用它查找 session 记录（message 历史、安全日志）
      - 由 POST /api/v1/sessions 创建，POST /api/v1/chat/{session_id} 使用

    生命周期：一轮完整对话（如"头疼 → 问诊 → 推荐 → 结束"）对应一个 session_id
    默认 30 分钟无活动自动过期
    """

    # ────────────────────────────────────────────
    # 对话历史
    # ────────────────────────────────────────────

    messages: Annotated[list[dict[str, Any]], add_messages]
    """【对话历史】完整的多轮对话记录。

    格式：每项为 {"role": "user"|"assistant"|"system", "content": "文本内容"}

    ⚠️ 特殊行为：
      - 使用 add_messages reducer：新消息会追加到已有列表末尾（不会覆盖）
      - LangGraph 自动把 dict 转成 LangChain 的 HumanMessage/AIMessage 对象，
        需要通过 normalize_messages() 转回纯 dict 再给 LLM
      - 每条消息在 End 节点会被持久化到 PostgreSQL messages 表

    示例：
      [
        {"role": "user", "content": "我头疼发烧"},
        {"role": "assistant", "content": "请问您体温多少度？持续多久了？"},
        {"role": "user", "content": "38度，2天了"},
      ]
    """

    # ────────────────────────────────────────────
    # 流程控制
    # ────────────────────────────────────────────

    phase: str
    """【当前阶段】标记 state 机当前在哪个大阶段。

    可选值：
      - "intake"       ← 初始状态，用户刚发来消息
      - "consulting"   ← 正在多轮症状追问中
      - "reacting"     ← ReactAgent 正在处理药品咨询/闲聊
      - "recommending" ← 已完成推荐
      - "ended"        ← 本轮处理结束

    用途：
      - Dispatcher 根据当前 phase 决定意图解析策略
      - End 节点会把它写入 message 的 metadata
      - SSE 事件不会直接用这个字段，而是用 node_events
    """

    previous_phase: str | None
    """【上一个阶段】记录跳转到新阶段之前的阶段。

    用途：预留字段，用于未来需要阶段恢复的场景。当前 v2 架构中
          由 Orchestrator 编排所有流程，不再需要条件跳转恢复。

    为 None 的情况：
      - 还没发生跨阶段跳转
      - 用户说"算了/去医院"（正常结束）
    """

    # ────────────────────────────────────────────
    # Dispatcher（路由分发）输出
    # ────────────────────────────────────────────

    dispatcher_result: dict[str, Any]
    """【Dispatcher 输出】LLM 分析用户意图后返回的执行计划（v2）。

    Dispatcher 只负责意图解析，不判断信息充分性。
    输出为有序动作列表 actions[]，由 Orchestrator 按优先级顺序执行。

    结构：
      {
        "actions": [
          {"action": "workflow", "intent": "describe_symptom", "priority": 1},
            # action 类型：
            #   workflow  → 症状求药主链路（consult → safety → recommend → inventory）
            #   react     → 通用对话（药品查询/对比/闲聊/放弃，由 ReactAgent 处理）
            # intent 分类：
            #   workflow: describe_symptom | answer_question | want_recommend | switch_drug
            #   react:    ask_drug | compare_drugs | ask_interaction | chat | give_up
            # priority: 1=先执行(workflow), 2=后执行(react)
          {"action": "react", "intent": "ask_drug", "query": "布洛芬有什么作用", "priority": 2}
            # query: react action 时的核心问题文本。workflow 时可为空
        ]
      }

    来源：Dispatcher 节点（LLM 调用）
    消费：Orchestrator 节点（按 actions 顺序编排 workflow 和 react）
    """

    # ────────────────────────────────────────────
    # Consult（症状问诊）输出
    # ────────────────────────────────────────────

    consult_slots: dict[str, Any]
    """【症状槽位】收集到的用户症状结构化信息。

    Consult Agent 通过多轮追问逐步填充这些槽位。每个槽位独立可空。

    结构：
      {
        "symptoms": [                          # ← 主要症状列表（每项可详细可简洁）
          {"name": "头痛", "location": "额头", "severity": "中度", "onset": "2天前"}
          # 简洁形式也合法：{"name": "发烧"}
        ],
        "temperature": 38.5,                   # ← 体温（℃），float，可为 null
        "duration_days": 3,                    # ← 症状持续天数，int，可为 null
        "medications_taken": ["布洛芬"],        # ← 已服用过的药物名称列表
        "special_population": "孕妇",           # ← 特殊人群标记，"孕妇"|"哺乳期"|"儿童"|"老人"|null
        "age": 28,                             # ← 年龄，int，可为 null
        "chronic_conditions": ["胃溃疡"],        # ← 慢性病史（影响用药安全性）
        "allergies": ["阿司匹林"],              # ← 药物过敏史
      }

    所有症状统一放入 symptoms 列表，不区分「主诉」和「伴随」。
    每个症状 dict 可包含：name（必填）、location、severity、onset。

    安全规则"信息充分"标准（三个必填维度）：
      1️⃣ symptoms（主要症状）— 至少知道症状名称
      2️⃣ duration_days（持续时间）— 帮助判断病情严重程度
      3️⃣ special_population（特殊人群）— 孕妇/儿童/老人用药需特别谨慎

    如果用户拒绝回答某维度，可跳过；三个必填满足后判定 info_sufficient → done

    来源：Consult Agent（LLM 调用）
    消费：SafetyCheck（安全规则引擎）、Recommend（症状 → 药品匹配）
    """

    consult_next_action: str
    """【Consult 下一步动作】本轮 Consult 后的决定。

    可选值：
      - "ask"  ← 信息还不够，需要继续追问（本轮只输出追问语，等用户下一轮回复）
      - "done" ← 信息充分，进入 safety_block → recommend → inventory 链路

    来源：Consult Agent
    消费：Orchestrator 根据 next_action 决定走 safety_block 还是结束
    """

    consult_rounds: int
    """【问诊轮数】当前 consult 流程中系统已追问的轮数。

    每轮 consult_node 执行后 +1。用于：
      - Consult Agent 判断是否达到 max_rounds 上限
      - 替代从 messages 内容反推轮数的不可靠 hack

    重置条件：
      - Dispatcher 判定用户切换了症状主题（reset_slots=true），consult_node 重置为 0
      - 新 session 首次对话时自然为 0
    """

    consult_summary: str
    """【症状摘要】Consult 判定 done 时生成的一句话自然语言摘要。

    示例：
      "用户症状：头痛、发热38.5°C，持续3天，无药物过敏史，非特殊人群，无慢性病史"

    用途：
      - Recommend 节点用它作为 LLM prompt 输入
      - 帮助 LLM 理解用户整体情况，匹配最合适的药品

    当 consult_next_action="ask" 时，此字段为空字符串 ""
    """

    # ────────────────────────────────────────────
    # SafetyCheck（安全筛查）输出
    # ────────────────────────────────────────────

    safety_result: dict[str, Any] | None
    """【安全筛查结果】规则引擎对当前症状的检查结论。

    结构：
      {
        "verdict": "PASS" | "BLOCK",
          # PASS  ← 全部通过，继续推荐
          # BLOCK ← 触发拦截规则，直接终止推荐流程，返回就医警告
        "triggered_rules": [
          {"rule_id": "R1", "action": "BLOCK", "reason": "体温 39.5°C..."}
        ],
        "excluded_drugs": [],   # [已废弃] 药品级过滤已移到 Neo4j
        "message": "⚠️ 检测到..."  # ← BLOCK 时的警告文案（直接展示给用户）
      }

    来源：safety_block 节点（调用 RuleEngine.check()）
    消费：
      - Orchestrator 根据 verdict 决定是否继续推荐
      - End 节点写入 safety_logs 表做审计
      - SSE 通过 "safety" 事件推送给前端

    注意：药品级别的禁忌过滤（原 R6/R7）已移到 recommend_node，
    由 Neo4j 知识图谱 _filter_by_kg_contraindications() 完成。
    """

    # ────────────────────────────────────────────
    # Recommend 输出
    # ────────────────────────────────────────────

    recommendations: list[dict[str, Any]]
    """【推荐药品列表】症状匹配后的排序推荐结果。

    结构（每个元素）：
      {
        "drug_id": 1,                        # ← drugs 表主键
        "generic_name": "布洛芬",             # ← 药品通用名
        "match_reason": "适用于发热和头痛症状", # ← 一句通俗的推荐理由
        "score": 0.95                        # ← 推荐度 0~1，越高越推荐
      }

    特征：
      - 长度 1-3 个（LLM 在候选药品中排 Top 3）
      - 按 score 降序排列（最好的在最前）
      - 已由 Neo4j 知识图谱排除禁忌药品

    来源：Recommend 节点（LLM 调用 + DB 查询）
    消费：
      - Inventory 节点用它查库存
      - SSE 通过 "data" 事件推送给前端
    """

    # ────────────────────────────────────────────
    # 输出
    # ────────────────────────────────────────────

    response: str
    """【本轮回复文本】要返回给用户的最终自然语言回复。

    内容由各节点拼接：
      - Consult ask  → AI 追问语（如"请问您体温多少？"）
      - Consult done → 过渡语（如"好的，让我为您推荐..."）
      - Safety BLOCK → 就医警告文案
      - Recommend    → 推荐药品列表 + 免责声明
      - Explain      → 药品说明书格式化的解释
      - Inventory    → 库存情况（追加到已有 response 末尾）

    注意：
      - inventory 节点的 response 是在已有 response 基础上追加库存信息
        （因为它紧接 recommend 之后，需要把推荐理由和库存一起展示）
      - chat.py 把 response 按 token 分片通过 SSE 推送给前端
      - End 节点把 response 作为 assistant 消息写入 session 历史

    示例（完整推荐流程结束后）：
      "根据您的情况，为您推荐以下药品：\\n\\n"
      "1. **布洛芬**（芬必得、美林）\\n"
      "   适用于发热和头痛症状\\n\\n"
      "2. **对乙酰氨基酚**（泰诺林）\\n"
      "   温和退热，适合轻度头痛\\n\\n"
      "## 📦 库存情况\\n"
      "- **布洛芬缓释胶囊** | 0.3g×24粒 | 某某制药\\n"
      "  💰 ¥18.50 | ✅ 有货 | 📍 A-3-2\\n\\n"
      "---\\n"
      "📋 **免责声明**：本系统仅为辅助参考..."
    """

    _event_queue: Any
    """【内部】流式事件队列（asyncio.Queue），用于节点向 SSE 生成器推送实时事件。

    不持久化到快照。chat.py 在 graph 执行前注入，节点通过 state.get("_event_queue") 获取。
    为 None 时（如测试/CLI 场景），push_step/push_token 自动 no-op。
    """

    node_events: list[dict[str, Any]]
    """【节点事件日志】本轮 Graph 运行中各节点的元数据记录。

    结构（每个元素）：
      {"node": "recommend", "count": 3}
      {"node": "dispatcher", "route": "consult", "intent": "describe_symptom"}
      {"node": "safety_block", "verdict": "PASS", "triggered_rules": []}
      {"node": "end", "status": "ok"}

    用途：
      - chat.py 据此生成 SSE "node" 事件（推送给前端展示处理进度）
      - 调试时可以直接看到各节点的输出摘要

    注意：这只是元数据，不是给用户看的文本。用户看到的内容在 response 字段。
    每个节点处理完都会往这个列表 append 一条记录。
    """


# ──────────────────────────────────────────────────────────────────────────────
# LangChain 消息类型映射
# ──────────────────────────────────────────────────────────────────────────────
# LangGraph 的 add_messages reducer 会把普通 dict 的 message 转成 LangChain
# 的消息对象（HumanMessage / AIMessage / SystemMessage）。
# 但在跟 OpenAI-compatible LLM 交互时，需要标准 {"role", "content"} 格式的 dict。
# 这个映射表处理两种来源的角色到 OpenAI 角色的转换。

_LC_ROLE_MAP = {
    "human": "user",           # LangChain HumanMessage
    "user": "user",            # 已经是标准格式
    "ai": "assistant",         # LangChain AIMessage
    "assistant": "assistant",  # 已经是标准格式
    "system": "system",        # 系统 prompt 消息
    "tool": "tool",            # tool call 结果（暂未使用）
    "function": "function",    # 旧版 function call（暂未使用）
}


def normalize_messages(messages: list) -> list[dict]:
    """把 LangChain 消息对象转换回标准的 {"role": "...", "content": "..."} dict。

    为什么需要这个函数：
      1. LangGraph 的 add_messages reducer 自动把 dict 转成 LangChain 消息对象
      2. 我们保存到 DB 的是 dict，也期望从 state.messages 取出的是 dict
      3. 但实际取出来可能是 HumanMessage/AIMessage 对象 → 需要转回来

    调用场景：
      - Dispatcher 节点：取 messages 给 LLM 分析意图
      - Consult Agent：取 messages 给 LLM 做症状追问
      - 任何需要把 state.messages 传给 LLM 的地方

    Args:
        messages: state.get("messages") 拿到的列表，类型混合（dict 或 LangChain 对象）

    Returns:
        全部为 {"role": "...", "content": "..."} 格式的 list[dict]
    """
    result = []
    for m in messages:
        if isinstance(m, dict):
            # 已经是 dict，只需统一 role 名称
            role = m.get("role", "user")
            role = _LC_ROLE_MAP.get(role, role)
            result.append({"role": role, "content": str(m.get("content", ""))})
        else:
            # LangChain 消息对象 → 取 type 属性判断角色
            lc_type = getattr(m, "type", "unknown")
            role = _LC_ROLE_MAP.get(lc_type, lc_type)
            content = getattr(m, "content", "")
            result.append({"role": role, "content": str(content)})
    return result


def initial_state(
    session_id: str,
    messages: list[dict] | None = None,
    snapshot: dict | None = None,
) -> ConversationState:
    """创建一个新的 turn 初始状态。

    每次用户发消息时调用，用 session_id 和历史 messages 初始化 state。
    Graph 会在这个初始状态上运行一个完整的 turn。

    Args:
        session_id: 会话 UUID
        messages:  该 session 的历史对话（从 DB 加载），首次对话传 []
                   格式为 [{"role": "user", "content": "..."}, ...]
        snapshot:  上一 turn 结束时保存的结构化状态快照（从 DB state_snapshot 字段加载）。
                   包含 consult_slots, phase, consult_rounds 等字段。
                   首次对话时为 None → 使用默认空值。

    Returns:
        填充了默认值的 ConversationState。如果提供了 snapshot，会用快照中的值覆盖默认空值，
        从而让结构化状态（slots, phase, rounds 等）跨 turn 存活。
    """
    state = ConversationState(
        session_id=session_id,
        messages=messages or [],
        phase="intake",
        previous_phase=None,
        consult_slots={
            "symptoms": [],
            "temperature": None,
            "duration_days": None,
            "medications_taken": [],
            "special_population": None,
            "age": None,
            "chronic_conditions": [],
            "allergies": [],
        },
        dispatcher_result={},
        consult_next_action="ask",
        consult_rounds=0,
        consult_summary="",
        safety_result=None,
        recommendations=[],
        response="",
        node_events=[],
        _event_queue=None,
    )

    # ── 从快照恢复结构化状态 ──
    # messages 已经从 DB 独立加载，不需要从快照恢复。
    # 其他字段如果快照中有就用快照的值覆盖默认值。
    if snapshot:
        _restorable_keys = (
            "consult_slots",
            "phase",
            "previous_phase",
            "consult_rounds",
            "consult_summary",
            "safety_result",
            "recommendations",
            "dispatcher_result",
        )
        for key in _restorable_keys:
            if key in snapshot:
                state[key] = snapshot[key]

    return state
