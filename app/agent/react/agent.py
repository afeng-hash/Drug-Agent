"""
ReactAgent — 工具驱动的 ReAct 循环 Agent。

处理所有非"症状求药"的泛咨询场景：
  - 药品查询（副作用、禁忌、用法用量）
  - 药品对比（"布洛芬和对乙酰氨基酚哪个好"）
  - 药物相互作用（"这两个能一起吃吗"）
  - "这个药"指代（通过 get_recommendation 工具解析）
  - 闲聊/感谢/放弃

ReAct 循环：
  LLM decide → tool_calls? → execute tools → append results → loop
             → text → final_response

独立性：不 import ConversationState，不依赖 LangGraph。可独立单元测试。
"""

import json
import logging
import time
from typing import Any

from app.agent.react.memory import WorkingMemory
from app.agent.react.schemas import AgentResult, AgentStep, ToolCall, ToolResult
from app.agent.react.tools import ToolRegistry
from app.llm.client import LLMClient
from app.llm.profile import LLMProfile

logger = logging.getLogger(__name__)

# ── 强制总结提示 ────────────────────────────────────────

_FORCE_SUMMARIZE_PROMPT = (
    "你已进行了多轮工具调用。请基于以上所有获取到的信息，"
    "用自然语言给用户一个完整的最终回复。不要再调用工具，直接回复。"
)

# ── 降级回复模板 ────────────────────────────────────────

_FALLBACK_TEMPLATE = """根据查询到的信息：

{findings}

（以上信息由系统自动查询整理，如需更详细的信息，建议咨询医生或药师。）"""


class ReactAgent:
    """ReAct Agent — LLM 驱动的工具调用循环。

    使用方式：
        agent = ReactAgent(
            llm_client=llm_client,
            system_prompt=REACT_SYSTEM_PROMPT,
            tool_registry=registry,
            profile=settings.get_profile("llm_react"),
            max_iterations=5,
        )
        result = await agent.run(
            user_message="布洛芬有什么副作用",
            conversation_history=[...],
            context=None,
        )
        print(result.final_response)
    """

    def __init__(
        self,
        llm_client: LLMClient,
        system_prompt: str,
        tool_registry: ToolRegistry,
        profile: LLMProfile | None = None,
        max_iterations: int = 5,
    ):
        """初始化 ReactAgent。

        Args:
            llm_client:      LLM 客户端（已配置 base_url + api_key）
            system_prompt:   Agent 系统提示词（角色定义 + 行为约束 + 工具使用指南）
            tool_registry:   工具注册中心
            profile:         场景 LLMProfile。None 时使用 LLMClient 的默认 profile
            max_iterations:  ReAct 循环最大迭代次数。超过后强制总结
        """
        self.llm_client = llm_client
        self.system_prompt = system_prompt
        self.tool_registry = tool_registry
        self.profile = profile
        self.max_iterations = max_iterations
        self.memory = WorkingMemory()

    # ── 主入口 ────────────────────────────────────────────

    async def run(
        self,
        user_message: str,
        history: list[dict[str, str]] | None = None,
        context: dict[str, Any] | None = None,
        event_queue: Any = None,
        on_token: Any = None,
    ) -> AgentResult:
        """执行 ReAct 循环，返回 AgentResult。

        Args:
            user_message: 用户当前输入文本
            history:      对话历史（[{"role":"user"|"assistant","content":"..."}]）
                          注意：应为 dict 格式，非 LangGraph 消息对象
            context:      动态上下文。通常为 workflow 的输出信息：
                          {"workflow_action": "done"|"ask",
                           "workflow_response": "..."}
            event_queue:  流式事件队列（asyncio.Queue），用于推送
                          tool_call/tool_result step 事件
            on_token:     文本 token 回调（async callable）。
                          用于 ReAct 最终回复的真流式推送。

        Returns:
            AgentResult — final_response + steps + 耗时统计
        """
        start_time = time.perf_counter()
        self.memory.clear()
        steps: list[AgentStep] = []

        # 构建初始消息
        messages = self._build_messages(user_message, history, context)

        # 获取工具定义（OpenAI format）
        tool_defs = self.tool_registry.get_definitions()

        try:
            for iteration in range(1, self.max_iterations + 1):
                # 流式 LLM 调用（带 tools + on_token 实时推送文本）
                result = await self.llm_client.generate_with_tools_stream(
                    messages=messages,
                    tools=tool_defs,
                    on_token=on_token,
                    profile=self.profile,
                    node="react",
                )

                # ── 情况 1: LLM 请求工具调用 ──
                if result.has_tool_calls:
                    step = await self._handle_tool_calls_stream(
                        iteration, result.tool_calls, messages, event_queue,
                    )
                    steps.append(step)
                    continue

                # ── 情况 2: LLM 返回纯文本（最终回复，token 已通过 on_token 实时推送） ──
                final_response = result.content or ""
                elapsed_ms = (time.perf_counter() - start_time) * 1000

                return AgentResult(
                    final_response=final_response,
                    steps=steps,
                    total_iterations=iteration,
                    total_time_ms=round(elapsed_ms, 2),
                )

            # ── 超过 max_iterations ──
            logger.warning(
                "ReactAgent exceeded max_iterations=%d, forcing summarize",
                self.max_iterations,
            )
            final_response = await self._force_summarize(messages, on_token)
            elapsed_ms = (time.perf_counter() - start_time) * 1000

            return AgentResult(
                final_response=final_response,
                steps=steps,
                total_iterations=self.max_iterations,
                total_time_ms=round(elapsed_ms, 2),
            )

        except Exception as e:
            # LLM 完全不可用时的降级
            logger.error("ReactAgent LLM call failed: %s", e)
            final_response = self._format_raw_result(str(e))
            elapsed_ms = (time.perf_counter() - start_time) * 1000

            return AgentResult(
                final_response=final_response,
                steps=steps,
                total_iterations=len(steps),
                total_time_ms=round(elapsed_ms, 2),
            )

    # ── 消息构建 ──────────────────────────────────────────

    def _build_messages(
        self,
        user_message: str,
        history: list[dict[str, str]] | None,
        context: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        """构建传给 LLM 的初始消息列表。

        Returns:
            [system_msg, ...history[-10:], user_msg]
        """
        messages: list[dict[str, Any]] = []

        # 1. System prompt（可能注入动态上下文）
        system_content = self.system_prompt
        if context:
            context_text = self._build_context_text(context)
            if context_text:
                system_content = system_content + "\n\n" + context_text

        messages.append({"role": "system", "content": system_content})

        # 2. 对话历史（最近 10 条）
        if history:
            # 标准化：确保每条是 dict，截断最近 10 条
            normalized = _normalize_history(history)
            messages.extend(normalized[-10:])

        # 3. 当前用户消息
        messages.append({"role": "user", "content": user_message})

        return messages

    def _build_context_text(self, context: dict[str, Any]) -> str:
        """构建动态上下文段落（注入到 system prompt 末尾）。

        仅当 workflow 先执行过时才注入，告诉 react agent：
          - workflow 刚才输出了什么
          - workflow 的完成状态（done/ask）
          - 如何自然衔接
        """
        workflow_action = context.get("workflow_action", "")
        workflow_response = context.get("workflow_response", "")

        if not workflow_response:
            return ""

        lines = [
            "## 对话上下文",
            "在你之前，系统的症状问诊流程刚刚完成，已经回复了用户以下内容：",
            "---",
            workflow_response,
            "---",
            "",
            "你的回复需要自然地衔接到这段内容之后：",
        ]

        if workflow_action == "ask":
            lines.append(
                "- 系统正在追问用户更多信息，你的回复要简短，"
                "不要打断追问流程，先简要回应，再过渡到回答用户的新问题"
            )
        elif workflow_action == "done":
            lines.append(
                "- 系统已经给出了药品推荐，你可以利用推荐结果做更精准的回答"
            )

        lines.append('- 使用自然的过渡语，避免生硬的“另外”“此外”')

        return "\n".join(lines)

    # ── 工具调用处理 ──────────────────────────────────────

    async def _handle_tool_calls(
        self,
        iteration: int,
        assistant_message: Any,
        messages: list[dict[str, Any]],
        event_queue: Any = None,
    ) -> AgentStep:
        """处理 LLM 返回的 tool_calls。

        1. 解析 tool_calls 为 ToolCall 列表
        2. 并行执行所有工具
        3. 将 assistant message + tool results 追加到 messages
        4. 缓存结果到 memory
        5. 推送 tool_call / tool_result step 事件到 event_queue
        6. 返回 AgentStep
        """
        from app.api.routes.stream_events import push_step

        # 解析 tool_calls
        raw_calls = assistant_message.tool_calls
        tool_calls: list[ToolCall] = []
        for tc in raw_calls:
            tool_calls.append(ToolCall(
                id=tc.id,
                tool_name=tc.function.name,
                arguments=_safe_json_parse(tc.function.arguments),
            ))

        # 将 assistant message（含 tool_calls）追加到 messages
        messages.append(_message_to_dict(assistant_message))

        # 并行执行工具
        tool_results: list[ToolResult] = []
        for tc in raw_calls:
            name = tc.function.name
            args = _safe_json_parse(tc.function.arguments)

            # ── 推送 tool_call step 事件 ──
            tool_label = _tool_label(name)
            arg_summary = _arg_summary(args)
            await push_step(
                event_queue, "react", "tool_call",
                f"调用工具: {tool_label}{arg_summary}",
                {"tool": name, "args": args},
            )

            result = await self.tool_registry.execute(name, args)

            # 缓存成功结果
            if result.success:
                self.memory.add_finding(name, result.data)

            tool_results.append(result)

            # ── 推送 tool_result step 事件 ──
            if result.success:
                result_summary = _result_summary(result.data)
                await push_step(
                    event_queue, "react", "tool_result",
                    f"{tool_label}: {result_summary}",
                    {"tool": name, "success": True, "summary": result_summary},
                )
            else:
                await push_step(
                    event_queue, "react", "tool_result",
                    f"{tool_label}: 执行失败 — {result.error or '未知错误'}",
                    {"tool": name, "success": False, "error": result.error},
                )

            # 包装工具结果（空结果标记 + 异常处理）
            wrapped = _wrap_tool_result(result)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(wrapped, ensure_ascii=False),
            })

        return AgentStep(
            iteration=iteration,
            thought=None,
            tool_calls=tool_calls,
            tool_results=tool_results,
        )

    async def _handle_tool_calls_stream(
        self,
        iteration: int,
        tool_calls_dicts: list[dict],
        messages: list[dict[str, Any]],
        event_queue: Any = None,
    ) -> AgentStep:
        """处理 generate_with_tools_stream 返回的 tool_calls（dict 格式）。

        与 _handle_tool_calls 逻辑相同，但入参是 StreamWithToolsResult.tool_calls
        （list[dict]）而非 OpenAI message 对象。
        """
        from app.api.routes.stream_events import push_step

        # 解析 tool_calls
        tool_calls: list[ToolCall] = []
        for tc in tool_calls_dicts:
            tool_calls.append(ToolCall(
                id=tc.get("id", ""),
                tool_name=tc.get("function", {}).get("name", ""),
                arguments=_safe_json_parse(tc.get("function", {}).get("arguments", "{}")),
            ))

        # 将 assistant message（含 tool_calls）追加到 messages
        messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": tool_calls_dicts,
        })

        # 并行执行工具
        tool_results: list[ToolResult] = []
        for tc in tool_calls_dicts:
            name = tc.get("function", {}).get("name", "")
            args = _safe_json_parse(tc.get("function", {}).get("arguments", "{}"))

            # ── 推送 tool_call step 事件 ──
            tool_label = _tool_label(name)
            arg_summary = _arg_summary(args)
            await push_step(
                event_queue, "react", "tool_call",
                f"调用工具: {tool_label}{arg_summary}",
                {"tool": name, "args": args},
            )

            result = await self.tool_registry.execute(name, args)

            if result.success:
                self.memory.add_finding(name, result.data)

            tool_results.append(result)

            # ── 推送 tool_result step 事件 ──
            if result.success:
                result_summary = _result_summary(result.data)
                await push_step(
                    event_queue, "react", "tool_result",
                    f"{tool_label}: {result_summary}",
                    {"tool": name, "success": True, "summary": result_summary},
                )
            else:
                await push_step(
                    event_queue, "react", "tool_result",
                    f"{tool_label}: 执行失败 — {result.error or '未知错误'}",
                    {"tool": name, "success": False, "error": result.error},
                )

            # 包装工具结果并追加到 messages
            wrapped = _wrap_tool_result(result)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "content": json.dumps(wrapped, ensure_ascii=False),
            })

        return AgentStep(
            iteration=iteration,
            thought=None,
            tool_calls=tool_calls,
            tool_results=tool_results,
        )

    # ── 降级处理 ──────────────────────────────────────────

    async def _force_summarize(
        self,
        messages: list[dict[str, Any]],
        on_token: Any = None,
    ) -> str:
        """超过 max_iterations 时，强制 LLM 基于已有信息总结。"""
        messages.append({"role": "system", "content": _FORCE_SUMMARIZE_PROMPT})

        try:
            result = await self.llm_client.generate_with_tools_stream(
                messages=messages,
                tools=[],  # 不带工具，强制 LLM 只做文本回复
                on_token=on_token,
                profile=self.profile,
                node="react",
            )
            return result.content or "抱歉，我无法完成这次查询。请稍后再试。"
        except Exception as e:
            logger.error("Force summarize failed: %s", e)
            return self._format_raw_result(str(e))

    def _format_raw_result(self, error_info: str = "") -> str:
        """LLM 完全不可用时的降级回复。

        有工具缓存数据时输出结构化摘要；无数据时做纯服务不可用提示。
        """
        findings = self.memory.snapshot()["intermediate_findings"]
        if not findings:
            return (
                "抱歉，当前服务暂时不可用，无法完成您的查询。"
                "建议您查看药品纸质说明书，或咨询医生/药师获取准确信息。"
            )

        parts: list[str] = []
        for tool_name, data in findings.items():
            if isinstance(data, list):
                items = []
                for item in data[:3]:
                    if isinstance(item, dict):
                        label = item.get("generic_name") or item.get("name") or item.get("title") or str(item)
                        items.append(label)
                    else:
                        items.append(str(item))
                parts.append(f"- {tool_name}: {', '.join(items)}")
            elif isinstance(data, dict):
                label = data.get("generic_name") or data.get("name") or str(data)
                parts.append(f"- {tool_name}: {label}")
            elif isinstance(data, str):
                parts.append(f"- {tool_name}: {data}")

        findings_text = "\n".join(parts) if parts else "暂无"

        return _FALLBACK_TEMPLATE.format(findings=findings_text)


# ── 辅助函数 ────────────────────────────────────────────

# LangChain → OpenAI 角色名映射（与 app/graph/state.py 的 _LC_ROLE_MAP 保持一致）
_LC_ROLE_MAP = {
    "human": "user",
    "user": "user",
    "ai": "assistant",
    "assistant": "assistant",
    "system": "system",
    "tool": "tool",
    "function": "function",
}


def _normalize_history(history: list[dict[str, str]]) -> list[dict[str, str]]:
    """标准化对话历史为纯 dict 格式。

    LangGraph 的 checkpoint 可能把消息存为 LangChain 对象，
    需要统一转为 {"role": "...", "content": "..."} 格式，
    并将 LangChain 角色名（human/ai）映射为 OpenAI 标准角色名（user/assistant）。
    """
    result: list[dict[str, str]] = []
    for msg in history:
        if not isinstance(msg, dict):
            # 可能是 LangChain 消息对象，提取 type + content，再映射 role
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


def _message_to_dict(message: Any) -> dict[str, Any]:
    """把 OpenAI message 对象转为 dict（用于追加到 messages 列表）。"""
    if hasattr(message, "model_dump"):
        return message.model_dump(exclude_none=True)
    if isinstance(message, dict):
        return message
    return {"role": "assistant", "content": str(message)}


def _safe_json_parse(raw: str | dict) -> dict:
    """安全解析 JSON 字符串为 dict。"""
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def _tool_label(tool_name: str) -> str:
    """将工具名映射为人类可读的标签。"""
    labels = {
        "search_drug": "查询药品信息",
        "get_drug_detail": "获取药品详情",
        "search_manual": "检索说明书",
        "search_web": "联网搜索",
        "check_inventory": "查询库存",
        "find_drug_location": "查询货架位置",
        "get_recommendation": "获取推荐列表",
        "get_user_profile": "获取用户信息",
    }
    return labels.get(tool_name, tool_name)


def _arg_summary(args: dict) -> str:
    """生成工具参数的人类可读摘要。"""
    if not args:
        return ""
    # 提取关键参数（跳过 internal 参数）
    key_args = {k: v for k, v in args.items()
                if not k.startswith("_") and v}
    if not key_args:
        return ""
    parts = []
    for k, v in key_args.items():
        if isinstance(v, str) and len(v) > 30:
            v = v[:27] + "..."
        parts.append(f"{k}={v}")
    return " (" + ", ".join(parts) + ")"


def _result_summary(data) -> str:
    """生成工具返回结果的摘要。"""
    if data is None:
        return "无数据"
    if isinstance(data, list):
        if len(data) == 0:
            return "未找到结果"
        return f"找到 {len(data)} 条记录"
    if isinstance(data, dict):
        if data.get("error"):
            return f"错误: {data['error']}"
        if data.get("empty") or data.get("found") is False:
            return "未找到结果"
        if data.get("source") == "web":
            results = data.get("results", [])
            return f"搜索到 {len(results)} 条网页"
        return f"获取到药品信息 ({len(data)} 字段)"
    return "获取到数据"


def _wrap_tool_result(result: ToolResult) -> dict:
    """包装工具结果为 LLM 友好格式。

    处理三种情况：
      1. 成功 + 非空数据 → 原样返回
      2. 成功 + 空数据（[]/{}) → 包装为 {"found": false, ...}
      3. 失败 → 返回 {"error": ...}
    """
    if not result.success:
        return {"error": result.error or "工具执行失败"}

    data = result.data

    # 空列表
    if isinstance(data, list) and len(data) == 0:
        return {
            "found": False,
            "results": [],
            "message": "本地知识库未找到相关信息。请尝试其他工具或联网搜索。",
        }

    # 空字典
    if isinstance(data, dict) and len(data) == 0:
        return {
            "found": False,
            "message": "未找到相关信息。",
        }

    # 字典中明确标记 empty
    if isinstance(data, dict) and data.get("empty") is True:
        return {
            "found": False,
            "message": data.get("message", "未找到相关信息。"),
        }

    # 正常数据（如果 dict 没有 found 字段，添加 found: true）
    if isinstance(data, dict) and "found" not in data:
        data = dict(data)
        data["found"] = True

    return data
