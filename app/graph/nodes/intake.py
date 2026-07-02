"""
Intake node — 用户消息预处理。

这是每个 Graph turn 的第一个节点。目前逻辑很简单：更新 phase 标记。
后续可以扩展为消息清洗、敏感词过滤等。
"""

from app.graph.state import ConversationState


async def intake_node(state: ConversationState) -> dict:
    """预处理用户消息，标记 turn 开始。

    Args:
        state: 当前会话状态

    Returns:
        state 更新：phase 置为 "intake"，记录节点事件
    """
    return {
        "phase": "intake",
        "node_events": [{"node": "intake", "status": "ok"}],
    }
