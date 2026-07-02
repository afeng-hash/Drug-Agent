"""LLM Client — OpenAI-compatible protocol via openai SDK."""

import json
from typing import Any, AsyncGenerator, Type, TypeVar

from openai import AsyncOpenAI
from pydantic import BaseModel

from app.config import Settings

T = TypeVar("T", bound=BaseModel)


class LLMClient:
    """Unified LLM client using OpenAI-compatible protocol.

    Supports: generate, generate_structured (JSON mode), stream, embed.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = AsyncOpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
        )
        self.model = settings.llm_model
        self.embedding_model = settings.embedding_model

    async def generate(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 1024,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Call chat completions and return the full response object as a dict."""
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
        """Call chat completions with JSON mode and parse into a Pydantic model.

        Falls back to tool_calling if the model does not support
        response_format json_object.
        """
        try:
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
            # Fallback: use tool calling
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
        """Stream chat completions, yielding content deltas."""
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
        """Generate embeddings for a list of texts."""
        response = await self.client.embeddings.create(
            model=self.embedding_model,
            input=texts,
        )
        return [item.embedding for item in response.data]


def _pydantic_to_tool(schema: Type[BaseModel], name: str) -> dict:
    """Convert a Pydantic model to an OpenAI tool/function definition."""
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
