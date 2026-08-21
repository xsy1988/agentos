"""runs / run_events 表（模块详细设计 §2.3，队列状态机与事件流）。"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPkMixin

UUID = PGUUID(as_uuid=True)

# 状态机：pending → running → (paused_awaiting_confirm) → done | failed | cancelled
RUN_STATUSES = (
    "pending",
    "running",
    "paused_awaiting_confirm",
    "done",
    "failed",
    "cancelled",
)


class Run(UUIDPkMixin, TimestampMixin, Base):
    """任务：队列状态机的载体。budget 为创建时从 Agent 配置固化的快照。"""

    __tablename__ = "runs"
    __table_args__ = (Index("ix_runs_status", "status"),)

    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, ForeignKey("conversations.id", ondelete="CASCADE")
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("agents.id", ondelete="RESTRICT"), index=True
    )
    trigger: Mapped[str] = mapped_column(String(16), default="manual")  # manual/timer/alarm
    status: Mapped[str] = mapped_column(String(32), default="pending")
    input: Mapped[dict] = mapped_column(JSONB, default=dict)
    result: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[dict | None] = mapped_column(JSONB)  # 结构化失败原因
    budget: Mapped[dict] = mapped_column(JSONB, default=dict)
    budget_used: Mapped[dict] = mapped_column(JSONB, default=dict)
    thread_ts: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunEvent(Base):
    """执行事件流。seq 为 run 内单调序号——SSE 断线续传游标。"""

    __tablename__ = "run_events"
    __table_args__ = (UniqueConstraint("run_id", "seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("runs.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
