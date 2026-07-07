"""
Admin Hook Registry — 可扩展的钩子注册中心。

提供统一的回调注册机制，供 admin 各模块在应用启动时注入钩子。
新模块只需在此注册回调，无需修改核心业务代码。

模式::

    # 在 register_admin_hooks() 中注册
    registry.register("llm_call", _write_llm_call_log)
    registry.register("high_risk_keyword", _check_high_risk_keywords)
    registry.register("trace_node", _write_trace_log)

    # 在业务代码中触发（fire-and-forget，不等待完成）
    registry.emit("llm_call", data=LLMCallLogData(...))

    # 需要确保写入时用 dispatch（等待所有回调完成）
    await registry.dispatch("llm_call", data=LLMCallLogData(...))
"""

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

# 回调签名：async def callback(**kwargs) -> None
HookCallback = Callable[..., Awaitable[None]]


class HookRegistry:
    """可扩展的钩子注册中心。

    特点：
      - 一个事件名可注册多个回调（按注册顺序依次调用）
      - emit(): fire-and-forget（失败不影响其他回调或主流程）
      - dispatch(): 等待所有回调完成（用于需要确保写入的场景）
      - 自动去重（同一事件 + 同一函数不会重复注册）
      - 支持内省（list_hooks()）和清理（clear()）
      - 线程安全（asyncio 单线程上下文）

    用法::

        registry = HookRegistry()

        # 注册（自动去重）
        registry.register("llm_call", my_logger)

        # Fire-and-forget 触发
        registry.emit("llm_call", model="gpt-4", tokens=42)

        # 等待完成的触发
        await registry.dispatch("llm_call", model="gpt-4", tokens=42)

        # 内省
        hooks = registry.list_hooks()
        # → [("llm_call", "_write_llm_call_log"), ...]

        # 清理
        registry.clear("llm_call")   # 清除特定事件
        registry.clear()             # 清除所有事件
    """

    def __init__(self) -> None:
        self._hooks: dict[str, list[HookCallback]] = defaultdict(list)

    # ── 注册 / 取消注册 ──────────────────────────────────────

    def register(self, event: str, callback: HookCallback) -> bool:
        """注册一个钩子回调（自动去重）。

        Args:
            event: 事件名，如 "llm_call", "high_risk_keyword", "trace_node"
            callback: 异步回调函数，签名 async def callback(**kwargs) -> None

        Returns:
            True 如果是新注册，False 如果回调已存在（去重跳过）。
        """
        if callback in self._hooks[event]:
            logger.debug(
                "Hook skipped (duplicate): %s → %s", event, callback.__name__,
            )
            return False
        self._hooks[event].append(callback)
        logger.debug("Hook registered: %s → %s", event, callback.__name__)
        return True

    def unregister(self, event: str, callback: HookCallback) -> bool:
        """取消注册一个钩子回调。

        Returns:
            True 如果成功移除，False 如果不存在。
        """
        try:
            self._hooks[event].remove(callback)
            logger.debug("Hook unregistered: %s → %s", event, callback.__name__)
            return True
        except (ValueError, KeyError):
            return False

    # ── 内省 / 清理 ──────────────────────────────────────────

    def list_hooks(self) -> list[tuple[str, str]]:
        """列出所有注册的钩子。

        Returns:
            [(event_name, callback_name), ...] 按注册顺序排列。
        """
        result: list[tuple[str, str]] = []
        for event, callbacks in self._hooks.items():
            for cb in callbacks:
                result.append((event, cb.__name__))
        return result

    def clear(self, event: str | None = None) -> int:
        """清除注册的钩子。

        Args:
            event: 要清除的事件名。None 表示清除所有事件。

        Returns:
            移除的回调数量。
        """
        if event is not None:
            count = len(self._hooks.pop(event, []))
            if count:
                logger.debug("Cleared %d hooks for event '%s'", count, event)
            return count
        else:
            total = sum(len(v) for v in self._hooks.values())
            self._hooks.clear()
            logger.debug("Cleared all %d hooks (%d events)", total,
                         len(self._hooks) if total else 0)
            return total

    # ── 触发 ──────────────────────────────────────────────────

    def emit(self, event: str, **kwargs: Any) -> list[asyncio.Task]:
        """Fire-and-forget 触发事件（立即返回，不等待回调完成）。

        所有注册的回调并发执行，单个回调失败不影响其他回调。
        返回的 Task 列表可用于取消（task.cancel()）。

        Args:
            event: 事件名
            **kwargs: 传递给回调的参数

        Returns:
            asyncio.Task 列表。调用方可持有以取消未完成的回调。
        """
        callbacks = self._hooks.get(event, [])
        if not callbacks:
            return []

        async def _safe_invoke(cb: HookCallback) -> None:
            try:
                await cb(**kwargs)
            except Exception:
                logger.debug(
                    "Hook callback %s for event '%s' failed",
                    cb.__name__, event, exc_info=True,
                )

        tasks = [asyncio.create_task(_safe_invoke(cb)) for cb in callbacks]
        return tasks

    async def dispatch(self, event: str, **kwargs: Any) -> None:
        """触发事件并等待所有回调完成。

        用于需要确保写入完成的场景（如测试中验证日志是否正确写入）。

        Args:
            event: 事件名
            **kwargs: 传递给回调的参数
        """
        callbacks = self._hooks.get(event, [])
        if not callbacks:
            return

        results = await asyncio.gather(
            *[cb(**kwargs) for cb in callbacks],
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                logger.debug("Hook callback failed: %s", r)


# ── 全局单例 ──

registry = HookRegistry()
"""全局钩子注册中心。

在 app/main.py lifespan 中调用 register_admin_hooks() 完成注册。
业务代码通过 registry.emit(...) 触发事件（fire-and-forget），
或 await registry.dispatch(...) 等待完成。
"""


# ── 便捷注册函数 ──

def register_admin_hooks() -> None:
    """注册所有 admin 钩子 + 桥接到核心模块（应用启动时调用）。

    这是唯一的注册入口。在 main.py 的 lifespan 中调用一次即可。
    多次调用安全（register() 自动去重）。

    职责:
      1. 将回调函数注册到 HookRegistry（供 emit/dispatch 使用）
      2. 桥接 registry → LLMClient._log_callback（供 client.py 使用）
      3. 桥接 registry → end_node._keyword_check_callback（供 end.py 使用）

    新增钩子在此函数中添加:
      1. registry.register("event_name", callback)
      2. 如需桥接到其他模块，在此完成
    """
    from app.api.routes.admin.hooks import (
        _check_high_risk_keywords,
        _write_llm_call_log,
    )
    from app.llm.client import LLMClient
    from app.graph.nodes.end import set_keyword_check_callback

    # ── 注册到 HookRegistry ──
    registry.register("llm_call", _write_llm_call_log)
    registry.register("high_risk_keyword", _check_high_risk_keywords)

    # 未来扩展示例：
    # registry.register("trace_node", _write_trace_log)
    # registry.register("user_action", _track_user_behavior)

    # ── 桥接 registry → 核心模块（仅设置一次）──
    # LLMClient._log_callback: 每次 LLM 调用 → registry.emit("llm_call")
    async def _llm_log_bridge(data) -> None:
        registry.emit("llm_call", data=data)

    LLMClient.set_log_callback(_llm_log_bridge)

    # end_node._keyword_check_callback: 每条消息 → registry.emit("high_risk_keyword")
    async def _keyword_check_bridge(session_id: str, content: str) -> None:
        registry.emit("high_risk_keyword", session_id=session_id, content=content)

    set_keyword_check_callback(_keyword_check_bridge)

    logger.info("Admin hooks registered: %s", registry.list_hooks())
