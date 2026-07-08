"""
Stream event helpers — 从 graph 节点推送 step/token 事件到 SSE 队列。

使用方式:
    from app.api.routes.stream_events import push_step, push_token

    q = state.get("_event_queue")
    await push_step(q, "recommend", "searching", "检索候选药品...", {"count": 12})
    await push_token(q, "布洛芬")

所有函数接受 queue=None（安全 no-op），无需调用方判空。
"""

import asyncio
from typing import Any


async def push_step(
    queue: asyncio.Queue | None,
    node: str,
    phase: str,
    message: str,
    data: dict[str, Any] | None = None,
) -> None:
    """推送一个 step 事件（Agent 行为轨迹）。

    前端以灰色文字展示，表示 Agent 当前在做什么。

    Args:
        queue:   asyncio.Queue（从 state["_event_queue"] 获取）
        node:    节点名 (dispatcher/consult/recommend/react)
        phase:   行为阶段标识，前端可用于选择图标
        message: 人类可读的描述文字
        data:    可选的结构化数据
    """
    if queue is None:
        return
    payload: dict[str, Any] = {
        "node": node,
        "phase": phase,
        "message": message,
    }
    if data:
        payload["data"] = data
    await queue.put({"type": "step", "data": payload})


async def push_token(queue: asyncio.Queue | None, content: str) -> None:
    """推送一个实时 token（LLM 流式输出）。

    前端以正常颜色打字机效果展示。这是给用户看的最终文本。

    Args:
        queue:   asyncio.Queue
        content: 单个 token 文本（通常是 1-5 个字符）
    """
    if queue is None:
        return
    await queue.put({"type": "token", "data": {"content": content}})


async def push_text_chunked(
    queue: asyncio.Queue | None,
    text: str,
    chunk_size: int = 5,
    delay: float = 0.02,
) -> None:
    """将已生成的文本分块推送到 token 流（用于结构化输出场景）。

    当 LLM 调用是结构化输出（generate_structured），无法逐 token 流式时，
    用此函数将完整文本切块推送，模拟打字机效果。

    Args:
        queue:      asyncio.Queue
        text:       要推送的完整文本
        chunk_size: 每块字符数（默认 5，比旧的 10 更流畅）
        delay:      块间延迟秒数
    """
    if queue is None or not text:
        return
    for i in range(0, len(text), chunk_size):
        await queue.put({
            "type": "token",
            "data": {"content": text[i:i + chunk_size]},
        })
        if delay > 0:
            await asyncio.sleep(delay)
