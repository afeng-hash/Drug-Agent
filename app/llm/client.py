"""
LLM Client — OpenAI-compatible protocol 封装。

通过 openai SDK 调用通义千问（DashScope）等 OpenAI-compatible 的 LLM 服务。
封装了四种常用模式：
  1. generate()            — 普通对话补全
  2. generate_structured() — 结构化输出（JSON Schema → Pydantic）
  3. generate_with_tools() — 带 tool definitions 的单次调用（供 ReactAgent 使用）
  4. generate_with_tools_stream() — 流式 + tools 调用（ReAct 真流式最终回复）
  5. stream()              — 流式输出（逐 token 返回）
  6. embed()               — 文本向量化（用于 RAG）

从 v2 开始，每个方法都接受可选的 LLMProfile 参数，实现按场景分离模型配置。
配置来源：app.config.Settings（llm_base_url, llm_api_key, llm_model, embedding_model）

日志采集：
  - LLMClient._log_callback 可由 admin 模块设置，实现 fire-and-forget 写入 llm_call_logs
  - 每个 generate 方法接受可选的 node 参数，标识调用节点（dispatcher/consult/react/...）
"""

import asyncio
import json
import time as time_module
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, TYPE_CHECKING, Type, TypeVar

from openai import AsyncOpenAI
from pydantic import BaseModel

from app.llm.profile import LLMProfile

if TYPE_CHECKING:
    from app.config import Settings

T = TypeVar("T", bound=BaseModel)


@dataclass
class LLMCallLogData:
    """传给 _log_callback 的 LLM 调用日志数据。"""
    node: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    success: bool = True
    error_message: str | None = None
    session_id: str | None = None
    turn_id: str | None = None


@dataclass
class StreamWithToolsResult:
    """generate_with_tools_stream() 的返回结果。"""
    has_tool_calls: bool = False
    tool_calls: list[dict] = field(default_factory=list)
    content: str = ""


class LLMClient:
    """统一的 LLM 客户端，基于 OpenAI-compatible 协议。

    使用方式：
        settings = Settings()
        client = LLMClient(settings)
        result = await client.generate([{"role": "user", "content": "你好"}])

        # 按场景使用不同 Profile
        profile = LLMProfile(model="qwen-turbo", temperature=0.1, max_tokens=256)
        result = await client.generate(messages, profile=profile)

        # 启用日志采集
        LLMClient.set_log_callback(my_async_callback)
    """

    # ── 模块级日志回调 ──
    _log_callback: Callable[[LLMCallLogData], Awaitable[None]] | None = None
    """由 admin 模块设置的异步回调，fire-and-forget 写入 llm_call_logs。"""

    @classmethod
    def set_log_callback(
        cls, callback: Callable[[LLMCallLogData], Awaitable[None]] | None,
    ) -> None:
        """设置日志回调（通常由 admin/__init__.py 在启动时调用）。"""
        cls._log_callback = callback

    def _schedule_log(self, data: LLMCallLogData) -> None:
        """Fire-and-forget 写入日志，不阻塞主流程。

        自动从 ContextVar 中补全 session_id / turn_id（若调用方未显式传入）。
        若 ContextVar 未设置（罕见：executor 场景中协程上下文丢失），记录 warning。
        """
        import logging
        _logger = logging.getLogger(__name__)
        if data.session_id is None:
            from app.llm.context import get_llm_session
            data.session_id = get_llm_session()
            if data.session_id is None:
                _logger.warning(
                    "_schedule_log: session_id ContextVar is None — "
                    "LLM call log will not be linked to a session. "
                    "This may happen if the coroutine context was lost (e.g., executor)."
                )
        if data.turn_id is None:
            from app.llm.context import get_llm_turn
            data.turn_id = get_llm_turn()
            if data.turn_id is None:
                _logger.warning(
                    "_schedule_log: turn_id ContextVar is None — "
                    "LLM call log will not be linked to a turn. "
                    "This may happen if the coroutine context was lost."
                )
        if LLMClient._log_callback:
            asyncio.create_task(LLMClient._log_callback(data))

    def __init__(self, settings: "Settings"):
        """初始化 LLM 客户端。

        Args:
            settings: 应用配置，包含 base_url、api_key、model 名称
        """
        self.settings = settings
        self.client = AsyncOpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
        )
        self.default_profile = LLMProfile(model=settings.llm_model)
        """默认 Profile。当方法未传入 profile 时使用（向后兼容）"""
        self.embedding_model = settings.embedding_model  # 嵌入模型（如 text-embedding-v3）

    async def generate(
        self,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        max_tokens: int | None = None,
        profile: LLMProfile | None = None,
        node: str | None = None,
        session_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """调用 chat completions API，返回完整响应对象的 dict。

        适用场景：Explain 节点（生成药品解释文本）、通用对话生成。

        Args:
            messages:    标准对话消息列表，[{"role": "user", "content": "..."}]
            temperature: 采样温度 0-2。None 时使用 profile 的默认值
            max_tokens:  最大输出 token 数。None 时使用 profile 的默认值
            profile:     场景配置。None 时使用 self.default_profile
            node:        调用节点标识（dispatcher|consult|react|recommend|...）
            session_id:  关联会话 UUID（可选）

        Returns:
            OpenAI API 响应的 model_dump()，结构：
            {"choices": [{"message": {"role": "assistant", "content": "..."}}], ...}
        """
        p = profile or self.default_profile
        model = p.model
        t0 = time_module.monotonic()
        try:
            response = await self.client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature if temperature is not None else p.temperature,
                max_tokens=max_tokens if max_tokens is not None else p.max_tokens,
                **kwargs,
            )
            result = response.model_dump()
            usage = result.get("usage", {}) or {}
            self._schedule_log(LLMCallLogData(
                node=node or "unknown",
                model=model,
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                latency_ms=(time_module.monotonic() - t0) * 1000,
                success=True,
                session_id=session_id,
            ))
            return result
        except Exception as exc:
            self._schedule_log(LLMCallLogData(
                node=node or "unknown",
                model=model,
                latency_ms=(time_module.monotonic() - t0) * 1000,
                success=False,
                error_message=str(exc),
                session_id=session_id,
            ))
            raise

    async def generate_structured(
        self,
        messages: list[dict[str, str]],
        schema: Type[T],
        temperature: float | None = None,
        max_tokens: int | None = None,
        profile: LLMProfile | None = None,
        node: str | None = None,
        session_id: str | None = None,
        **kwargs: Any,
    ) -> T:
        """调用 LLM 并解析为 Pydantic 结构化输出。

        适用场景：Dispatcher（路由决策）、Consult Agent（槽位更新）、
                  Recommend（药品排序）。

        工作流程：
          1. 先尝试用 response_format: json_object → 解析 JSON
          2. 如果失败（某些模型不支持 json_object 格式），
             降级用 tool_calling / function calling 方式

        Args:
            messages:    对话消息
            schema:      目标 Pydantic 模型类（如 DispatcherDecision）
            temperature: 采样温度。None 时使用 profile 默认值
            max_tokens:  最大输出 token 数。None 时使用 profile 默认值
            profile:     场景配置。None 时使用 self.default_profile
            node:        调用节点标识（dispatcher|consult|classifier|...）
            session_id:  关联会话 UUID（可选）

        Returns:
            解析后的 Pydantic 模型实例
        """
        p = profile or self.default_profile
        model = p.model
        temp = temperature if temperature is not None else p.temperature
        max_tok = max_tokens if max_tokens is not None else p.max_tokens
        t0 = time_module.monotonic()

        try:
            # ── 方式 1：JSON 模式 ──
            response = await self.client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temp,
                max_tokens=max_tok,
                response_format={"type": "json_object"},
                **kwargs,
            )
            raw = response.choices[0].message.content
            usage = response.usage
            self._schedule_log(LLMCallLogData(
                node=node or "unknown",
                model=model,
                prompt_tokens=usage.prompt_tokens if usage else 0,
                completion_tokens=usage.completion_tokens if usage else 0,
                latency_ms=(time_module.monotonic() - t0) * 1000,
                success=True,
                session_id=session_id,
            ))
            if raw is None:
                raise ValueError("LLM returned empty response")
            data = json.loads(raw)
            return schema.model_validate(data)

        except Exception as first_error:
            # ── 方式 2：Tool Calling 降级 ──
            # 记录 JSON mode 失败（供运维监控）
            import logging
            _logger = logging.getLogger(__name__)
            _logger.warning(
                "generate_structured JSON mode failed for schema=%s model=%s "
                "node=%s elapsed=%.0fms error=%s — falling back to tool calling",
                schema.__name__, model, node or "unknown",
                (time_module.monotonic() - t0) * 1000, first_error,
            )

            try:
                tool_name = schema.__name__
                tool_schema = _pydantic_to_tool(schema, tool_name)

                response = await self.client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=temp,
                    max_tokens=max_tok,
                    tools=[tool_schema],
                    tool_choice={"type": "function", "function": {"name": tool_name}},
                    **kwargs,
                )
                tool_call = response.choices[0].message.tool_calls[0]
                usage = response.usage
                self._schedule_log(LLMCallLogData(
                    node=node or "unknown",
                    model=model,
                    prompt_tokens=usage.prompt_tokens if usage else 0,
                    completion_tokens=usage.completion_tokens if usage else 0,
                    latency_ms=(time_module.monotonic() - t0) * 1000,  # 含 JSON mode 耗时
                    success=True,
                    session_id=session_id,
                ))
                data = json.loads(tool_call.function.arguments)
                return schema.model_validate(data)

            except Exception as second_error:
                self._schedule_log(LLMCallLogData(
                    node=node or "unknown",
                    model=model,
                    latency_ms=(time_module.monotonic() - t0) * 1000,
                    success=False,
                    error_message=str(second_error),
                    session_id=session_id,
                ))
                raise

    async def generate_with_tools(
        self,
        messages: list[dict[str, str]],
        tools: list[dict],
        temperature: float | None = None,
        max_tokens: int | None = None,
        profile: LLMProfile | None = None,
        tool_choice: str | dict | None = None,
        node: str | None = None,
        session_id: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """单次 LLM 调用，返回可能包含 tool_calls 的原始响应。

        适用场景：ReactAgent 的 ReAct 循环。每次调用只做一次请求，
        不循环——循环逻辑在 ReactAgent 中。

        Args:
            messages:    对话消息（含 system prompt + 历史 + 用户输入）
            tools:       OpenAI function calling 格式的 tool 定义列表
            temperature: 采样温度。None 时使用 profile 默认值
            max_tokens:  最大输出 token 数。None 时使用 profile 默认值
            profile:     场景配置。None 时使用 self.default_profile
            tool_choice: 工具选择策略。"auto" / "none" / {"type":"function","function":{"name":"..."}}
            node:        调用节点标识（react|...）
            session_id:  关联会话 UUID（可选）

        Returns:
            原始 OpenAI chat.completions.create 响应对象（不解包）。
            调用方检查 response.choices[0].message.tool_calls 判断是否请求了工具调用。
        """
        p = profile or self.default_profile
        model = p.model
        temp = temperature if temperature is not None else p.temperature
        max_tok = max_tokens if max_tokens is not None else p.max_tokens

        create_kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temp,
            "max_tokens": max_tok,
            "tools": tools,
            **kwargs,
        }
        if tool_choice is not None:
            create_kwargs["tool_choice"] = tool_choice

        t0 = time_module.monotonic()
        try:
            response = await self.client.chat.completions.create(**create_kwargs)
            usage = response.usage
            self._schedule_log(LLMCallLogData(
                node=node or "unknown",
                model=model,
                prompt_tokens=usage.prompt_tokens if usage else 0,
                completion_tokens=usage.completion_tokens if usage else 0,
                latency_ms=(time_module.monotonic() - t0) * 1000,
                success=True,
                session_id=session_id,
            ))
            return response
        except Exception as exc:
            self._schedule_log(LLMCallLogData(
                node=node or "unknown",
                model=model,
                latency_ms=(time_module.monotonic() - t0) * 1000,
                success=False,
                error_message=str(exc),
                session_id=session_id,
            ))
            raise

    async def generate_with_tools_stream(
        self,
        messages: list[dict[str, str]],
        tools: list[dict],
        on_token: Callable[[str], Awaitable[None]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        profile: LLMProfile | None = None,
        tool_choice: str | dict | None = None,
        node: str | None = None,
        session_id: str | None = None,
        **kwargs: Any,
    ) -> StreamWithToolsResult:
        """流式调用 LLM（支持 tools），同时通过 on_token 回调推送文本 token。

        与 generate_with_tools() 的区别：
          - 使用 stream=True，支持实时 token 推送（打字机效果）
          - 工具调用（tool_calls）在流中增量累积，文本内容实时回调
          - 兼容 OpenAI stream_options={"include_usage": True}

        Args:
            messages:    对话消息
            tools:       OpenAI function calling 格式的 tool 定义列表
            on_token:    文本 token 回调（仅文本内容触发，tool_call JSON 不触发）
            temperature: 采样温度
            max_tokens:  最大输出 token 数
            profile:     场景配置
            tool_choice: 工具选择策略
            node:        调用节点标识
            session_id:  关联会话 UUID

        Returns:
            StreamWithToolsResult: has_tool_calls / tool_calls / content
        """
        p = profile or self.default_profile
        model = p.model
        temp = temperature if temperature is not None else p.temperature
        max_tok = max_tokens if max_tokens is not None else p.max_tokens

        create_kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temp,
            "max_tokens": max_tok,
            "tools": tools,
            "stream": True,
            "stream_options": {"include_usage": True},
            **kwargs,
        }
        if tool_choice is not None:
            create_kwargs["tool_choice"] = tool_choice

        t0 = time_module.monotonic()
        prompt_tokens = 0
        completion_tokens = 0

        # 增量累积 tool_calls（OpenAI 流中分多个 chunk 传输 function name + arguments）
        tool_calls_acc: dict[int, dict] = {}  # index → {id, function: {name, arguments}}
        content_parts: list[str] = []

        try:
            stream = await self.client.chat.completions.create(**create_kwargs)

            async for chunk in stream:
                # usage chunk（choices 为空）
                if hasattr(chunk, "usage") and chunk.usage:
                    prompt_tokens = chunk.usage.prompt_tokens or 0
                    completion_tokens = chunk.usage.completion_tokens or 0
                    continue  # usage chunk 没有 choices，跳过后续处理

                if not chunk.choices:
                    continue

                delta = chunk.choices[0].delta
                if delta is None:
                    continue

                # ── 累积 tool_calls ──
                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        if idx not in tool_calls_acc:
                            tool_calls_acc[idx] = {
                                "id": tc_delta.id or "",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            }
                        acc = tool_calls_acc[idx]
                        if tc_delta.id:
                            acc["id"] = tc_delta.id
                        if tc_delta.function:
                            if tc_delta.function.name:
                                acc["function"]["name"] += tc_delta.function.name
                            if tc_delta.function.arguments:
                                acc["function"]["arguments"] += tc_delta.function.arguments

                # ── 文本内容 → 实时回调 ──
                if delta.content:
                    content_parts.append(delta.content)
                    if on_token:
                        await on_token(delta.content)

            # 按 index 排序 tool_calls
            tool_calls = [
                tool_calls_acc[i]
                for i in sorted(tool_calls_acc.keys())
            ]

            self._schedule_log(LLMCallLogData(
                node=node or "unknown",
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=(time_module.monotonic() - t0) * 1000,
                success=True,
                session_id=session_id,
            ))

            return StreamWithToolsResult(
                has_tool_calls=len(tool_calls) > 0,
                tool_calls=tool_calls,
                content="".join(content_parts),
            )

        except Exception as exc:
            self._schedule_log(LLMCallLogData(
                node=node or "unknown",
                model=model,
                latency_ms=(time_module.monotonic() - t0) * 1000,
                success=False,
                error_message=str(exc),
                session_id=session_id,
            ))
            raise

    async def stream(
        self,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        max_tokens: int | None = None,
        profile: LLMProfile | None = None,
        node: str | None = None,
        session_id: str | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]:
        """流式生成，逐 token 返回文本。

        适用场景：需要让用户实时看到 AI 逐字输出的场景。
        当前 MVP 未使用（用 SSE "token" 事件模拟了流式效果）。

        Args:
            messages:    对话消息
            temperature: 采样温度。None 时使用 profile 默认值
            max_tokens:  最大输出 token 数。None 时使用 profile 默认值
            profile:     场景配置。None 时使用 self.default_profile
            node:        调用节点标识
            session_id:  关联会话 UUID（可选）

        Yields:
            每个 token 的文本内容（str）
        """
        p = profile or self.default_profile
        model = p.model
        temp = temperature if temperature is not None else p.temperature
        max_tok = max_tokens if max_tokens is not None else p.max_tokens

        t0 = time_module.monotonic()
        prompt_tokens = 0
        completion_tokens = 0
        try:
            stream = await self.client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temp,
                max_tokens=max_tok,
                stream=True,
                stream_options={"include_usage": True},
                **kwargs,
            )
            async for chunk in stream:
                # 最后一个 chunk 通常包含 usage（取决于 provider 是否支持）
                # 注意：usage chunk 的 choices 可能为空数组，需先判断
                if hasattr(chunk, "usage") and chunk.usage:
                    prompt_tokens = chunk.usage.prompt_tokens or 0
                    completion_tokens = chunk.usage.completion_tokens or 0
                if chunk.choices and chunk.choices[0].delta:
                    delta = chunk.choices[0].delta
                    if delta.content:
                        yield delta.content

            self._schedule_log(LLMCallLogData(
                node=node or "unknown",
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=(time_module.monotonic() - t0) * 1000,
                success=True,
                session_id=session_id,
            ))
        except Exception as exc:
            self._schedule_log(LLMCallLogData(
                node=node or "unknown",
                model=model,
                latency_ms=(time_module.monotonic() - t0) * 1000,
                success=False,
                error_message=str(exc),
                session_id=session_id,
            ))
            raise

    async def generate_with_stream_callback(
        self,
        messages: list[dict[str, str]],
        on_token: Callable[[str], Awaitable[None]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        profile: LLMProfile | None = None,
        node: str | None = None,
        session_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        """流式生成 + 回调推送每个 token，返回完整文本。

        用于需要真正打字机效果的场景（recommend_node、ResponseGenerator 等）。
        每个 delta.content 通过 on_token 回调实时推送（供 SSE 使用），
        同时累积完整文本作为返回值。

        Args:
            messages:    对话消息
            on_token:    每个 token 的回调。None 时等同于 stream()
            temperature: 采样温度。None 时使用 profile 默认值
            max_tokens:  最大输出 token 数
            profile:     场景配置
            node:        调用节点标识
            session_id:  关联会话 UUID

        Returns:
            累积的完整响应文本
        """
        full_text: list[str] = []
        async for token in self.stream(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            profile=profile,
            node=node,
            session_id=session_id,
            **kwargs,
        ):
            full_text.append(token)
            if on_token:
                await on_token(token)
        return "".join(full_text)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """生成文本的向量嵌入。

        适用场景：RAG 检索（把用户查询转成向量，在 Milvus 中搜索相似文本）。

        Args:
            texts: 需要向量化的文本列表，如 ["布洛芬 副作用 不良反应"]

        Returns:
            嵌套列表，外层对应输入文本，内层是浮点向量。
            如 text-embedding-v3 返回 1024 维向量。
        """
        response = await self.client.embeddings.create(
            model=self.embedding_model,
            input=texts,
        )
        return [item.embedding for item in response.data]


def _pydantic_to_tool(schema: Type[BaseModel], name: str) -> dict:
    """把 Pydantic 模型转换为 OpenAI tool/function 定义。

    用于 generate_structured() 的降级方案（当 json_object 模式不可用时）。

    Args:
        schema: Pydantic 模型类
        name:   tool 名称

    Returns:
        OpenAI tool 定义 dict：
        {"type": "function", "function": {"name": "...", "description": "...", "parameters": {...}}}
    """
    json_schema = schema.model_json_schema()
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": json_schema.get("description", ""),
            "parameters": {
                "type": "object",
                "properties": json_schema.get("properties", {}),
                "required": json_schema.get("required", []),
            },
        },
    }
