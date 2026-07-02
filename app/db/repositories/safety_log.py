"""
Safety log repository — safety_logs 表的写入。

用于记录每次安全规则检查的结果，方便审计和问题追溯。
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SafetyLog


class SafetyLogRepository:
    """安全日志记录器。

    只在 End 节点使用，记录本轮安全检查的结果。
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    async def log(
        self,
        session_id: int,
        verdict: str,
        triggered_rules: list[dict],
        input_slots: dict,
    ) -> SafetyLog:
        """记录一条安全筛查结果。

        Args:
            session_id:      内部 session id（sessions 表的主键 int，不是 UUID）
            verdict:         结论 'PASS' / 'BLOCK' / 'FILTER'
            triggered_rules: 触发规则列表，
                             如 [{"rule_id":"r1","action":"BLOCK","reason":"..."}]
            input_slots:     触发时的症状快照

        Returns:
            新创建的 SafetyLog 对象
        """
        log_entry = SafetyLog(
            session_id=session_id,
            verdict=verdict,
            triggered_rules=triggered_rules,
            input_slots=input_slots,
        )
        self.db.add(log_entry)
        await self.db.commit()
        await self.db.refresh(log_entry)
        return log_entry
