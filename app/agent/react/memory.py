"""
WorkingMemory — ReactAgent 的短期工作记忆。

单次 ReactAgent 调用内使用，缓存工具调用结果避免重复调用，
记录 agent 的中间发现和上下文备注。
"""

from typing import Any


class WorkingMemory:
    """Agent 工作记忆（单次 run() 调用生命周期内）。

    与 schemas.py 中的 WorkingMemory Pydantic 模型对应：
      - 这里的 find/add/get/clear 是运行时操作
      - Pydantic 模型用于序列化和日志记录

    使用方式：
        mem = WorkingMemory()
        mem.add_finding("search_drug", [{"name": "布洛芬"}])
        data = mem.get_finding("search_drug")  # [{"name": "布洛芬"}]
    """

    def __init__(self):
        self._findings: dict[str, Any] = {}
        self._notes: list[str] = []

    # ── 工具结果缓存 ─────────────────────────────────────

    def add_finding(self, tool_name: str, data: Any) -> None:
        """记录一次工具调用的结果（缓存，避免重复调用）。"""
        self._findings[tool_name] = data

    def get_finding(self, tool_name: str) -> Any | None:
        """读取工具缓存的结果。未缓存时返回 None。"""
        return self._findings.get(tool_name)

    def has_finding(self, tool_name: str) -> bool:
        """是否已有该工具的缓存结果。"""
        return tool_name in self._findings

    # ── 备注 ─────────────────────────────────────────────

    def add_note(self, note: str) -> None:
        """添加一条上下文备注。"""
        self._notes.append(note)

    @property
    def notes(self) -> list[str]:
        """所有备注列表。"""
        return list(self._notes)

    # ── 序列化（用于日志/node_events） ───────────────────

    def snapshot(self) -> dict[str, Any]:
        """导出为可序列化的 dict。"""
        return {
            "intermediate_findings": dict(self._findings),
            "context_notes": list(self._notes),
        }

    # ── 生命周期 ─────────────────────────────────────────

    def clear(self) -> None:
        """重置所有记忆。"""
        self._findings.clear()
        self._notes.clear()

    @property
    def is_empty(self) -> bool:
        """是否有任何记录。"""
        return not self._findings and not self._notes
