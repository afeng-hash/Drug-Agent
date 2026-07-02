"""
LLM Client — OpenAI-compatible protocol 封装。

通过 openai SDK 调用通义千问（DashScope）等 OpenAI-compatible 的 LLM 服务。
封装了三种常用模式：
  1. generate()           — 普通对话补全
  2. generate_structured()— 结构化输出（JSON Schema → Pydantic）
  3. stream()             — 流式输出（逐 token 返回）
  4. embed()              — 文本向量化（用于 RAG）

配置来源：app.config.Settings（llm_base_url, llm_api_key, llm_model, embedding_model）
"""

import json
from typing import Any, AsyncGenerator, Type, TypeVar

from openai import AsyncOpenAI
from pydantic import BaseModel

from app.config import Settings

T = TypeVar("T", bound=BaseModel)


class LLMClient:
    """统一的 LLM 客户端，基于 OpenAI-compatible 协议。

    使用方式：
        settings = Settings()
        client = LLMClient(settings)
        result = await client.generate([{"role": "user", "content": "你好"}])
    """

    def __init__(self, settings: Settings):
        """初始化 LLM 客户端。

        Args:
            settings: 应用配置，包含 base_url、api_key、model 名称
        """
        self.settings = settings
        self.client = AsyncOpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
        )
        self.model = settings.llm_model                 # 对话模型（如 qwen-plus）
        self.embedding_model = settings.embedding_model  # 嵌入模型（如 text-embedding-v3）

    async def generate(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 1024,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """调用 chat completions API，返回完整响应对象的 dict。

        适用场景：Explain 节点（生成药品解释文本）。

        Args:
            messages:    标准对话消息列表，[{"role": "user", "content": "..."}]
            temperature: 采样温度 0-2。0.2-0.3 适合需要稳定输出的任务
            max_tokens:  最大输出 token 数

        Returns:
            OpenAI API 响应的 model_dump()，结构：
            {"choices": [{"message": {"role": "assistant", "content": "..."}}], ...}
        """
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )
        return response.model_dump()

    async def generate_structured(
        self,
        messages: list[dict[str, str]],
        schema: Type[T],
        temperature: float = 0.2,
        max_tokens: int = 1024,
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
            temperature: 采样温度
            max_tokens:  最大输出 token 数

        Returns:
            解析后的 Pydantic 模型实例
        """
        try:
            # ── 方式 1：JSON 模式 ──
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
                **kwargs,
            )
            raw = response.choices[0].message.content
            if raw is None:
                raise ValueError("LLM returned empty response")
            data = json.loads(raw)
            return schema.model_validate(data)

        except Exception:
            # ── 方式 2：Tool Calling 降级 ──
            # 把 Pydantic schema 转成 tool 的 parameters
            tool_name = schema.__name__
            tool_schema = _pydantic_to_tool(schema, tool_name)

            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                tools=[tool_schema],
                tool_choice={"type": "function", "function": {"name": tool_name}},
                **kwargs,
            )
            tool_call = response.choices[0].message.tool_calls[0]
            data = json.loads(tool_call.function.arguments)
            return schema.model_validate(data)

    async def stream(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 1024,
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]:
        """流式生成，逐 token 返回文本。

        适用场景：需要让用户实时看到 AI 逐字输出的场景。
        当前 MVP 未使用（用 SSE "token" 事件模拟了流式效果）。

        Args:
            messages:    对话消息
            temperature: 采样温度
            max_tokens:  最大输出 token 数

        Yields:
            每个 token 的文本内容（str）
        """
        stream = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
            **kwargs,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta
            if delta.content:
                yield delta.content

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
