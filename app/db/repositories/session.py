"""
Session repository — sessions 表的 CRUD。

管理匿名会话的创建、查询、消息追加。
会话过期机制：在 get() 查询时自动检测过期并标记 status='expired'。
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Message, Session


class SessionRepository:
    """会话管理器。

    Usage:
        repo = SessionRepository(db, expire_minutes=30)
        session = await repo.create()            # 新建会话
        session = await repo.get(session_id)     # 查询（自动处理过期）
        await repo.add_message(...)              # 添加消息
        await repo.close(session_id)             # 主动关闭
    """

    def __init__(self, db: AsyncSession, expire_minutes: int = 30):
        """初始化会话仓库。

        Args:
            db:              已绑定的数据库会话
            expire_minutes:  会话过期时间（分钟）。默认 30 分钟无活动即过期
        """
        self.db = db
        self.expire_minutes = expire_minutes

    async def create(self) -> Session:
        """创建一个新的匿名会话。

        session_id 自动生成为 UUID v4。
        expires_at = 当前时间 + expire_minutes 分钟。

        Returns:
            新创建的 Session 对象（已 commit + refresh）
        """
        now = datetime.now(timezone.utc)
        session = Session(
            session_id=str(uuid.uuid4()),
            status="active",
            expires_at=now + timedelta(minutes=self.expire_minutes),
            created_at=now,
            updated_at=now,
        )
        self.db.add(session)
        await self.db.commit()
        await self.db.refresh(session)
        return session

    async def get(self, session_id: str) -> Session | None:
        """按 session_id 查询会话。

        自动过期检测：如果 now > expires_at 且 status='active'，
        自动将 status 更新为 'expired' 并 commit。

        Args:
            session_id: 会话 UUID 字符串

        Returns:
            Session 对象，不存在返回 None
        """
        stmt = select(Session).where(Session.session_id == session_id)
        result = await self.db.execute(stmt)
        session = result.scalar_one_or_none()
        if session is None:
            return None

        # 自动过期：超过过期时间且还是 active → 标记为 expired
        now = datetime.now(timezone.utc)
        if session.expires_at < now and session.status == "active":
            session.status = "expired"
            session.updated_at = now
            await self.db.commit()
            await self.db.refresh(session)
        return session

    async def close(self, session_id: str) -> None:
        """主动关闭会话（如用户说"再见"）。

        Args:
            session_id: 会话 UUID
        """
        stmt = (
            update(Session)
            .where(Session.session_id == session_id)
            .values(status="closed", updated_at=datetime.now(timezone.utc))
        )
        await self.db.execute(stmt)
        await self.db.commit()

    async def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        intent: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        """向会话追加一条消息。

        同时更新 session 的 updated_at 时间戳（刷新过期倒计时）。

        Args:
            session_id: 会话 UUID
            role:       发言角色 'user' 或 'assistant'
            content:    消息正文
            intent:     用户意图标签（仅 user 消息填充），如 'describe_symptom'
            metadata:   扩展元数据，如 {"phase": "recommending"}

        Raises:
            ValueError: 如果 session 不存在
        """
        session = await self.get(session_id)
        if session is None:
            raise ValueError(f"Session {session_id} not found")

        message = Message(
            session_id=session.id,   # 用内部 int id 做外键
            role=role,
            content=content,
            intent=intent,
            metadata_=metadata,
        )
        self.db.add(message)

        # 刷新 updated_at，延长过期倒计时
        session.updated_at = datetime.now(timezone.utc)
        await self.db.commit()

    async def update_snapshot(self, session_id: str, snapshot: dict) -> None:
        """持久化结构化状态快照，供下个 turn 恢复。

        在 end_node 中调用，保存 consult_slots、phase、consult_rounds 等
        跨 turn 需要存活的结构化状态。

        Args:
            session_id: 会话 UUID
            snapshot:   状态快照 dict

        Raises:
            ValueError: 如果 session 不存在
        """
        session = await self.get(session_id)
        if session is None:
            raise ValueError(f"Session {session_id} not found")

        session.state_snapshot = snapshot
        session.updated_at = datetime.now(timezone.utc)
        await self.db.commit()
