"""conversations / messages 表（模块详细设计 §2.2）。

会话 = LangGraph thread：conversations.id 即 checkpointer 的 thread_id，一一对应。
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPkMixin

UUID = PGUUID(as_uuid=True)


class Conversation(UUIDPkMixin, TimestampMixin, Base):
    """id 即 LangGraph thread_id（数据库设计 §2.1）。"""

    __tablename__ = "conversations"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("agents.id", ondelete="RESTRICT"), index=True
    )
    title: Mapped[str] = mapped_column(String(255), default="新会话")
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Message(Base):
    """消息流。content 为 JSONB：{text, tool_calls[], attachments[], token_usage}。"""

    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_conversation_created", "conversation_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("conversations.id", ondelete="CASCADE")
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, ForeignKey("runs.id", ondelete="SET NULL")
    )
    role: Mapped[str] = mapped_column(String(16))  # user / assistant / system / tool
    content: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
