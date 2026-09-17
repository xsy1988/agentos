"""runs / run_events 表（模块详细设计 §2.3，队列状态机与事件流）。"""

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
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
    __table_args__ = (
        Index("ix_runs_status", "status"),
        # 巡检/补偿按截止时间扫"已超时但仍在跑"的 run
        Index("ix_runs_deadline_at", "deadline_at"),
    )

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
    # P0-1 时长账本：deadline_at 一次写入永不重置（分段执行累加不漂移）；
    # active_ms 只累加执行段时长，等待/暂停不计入；paused_at 为暂停顺延依据
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active_ms: Mapped[int | None] = mapped_column(Integer)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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


# 产物种类：text/json 走 inline payload；file 指向 files 表；link 存 URL
ARTIFACT_KINDS = ("text", "json", "file", "link")
# 存储方式：inline=payload 内联；pg=payload 大字段；file=files 表（file_id）
ARTIFACT_STORAGE = ("inline", "pg", "file")


class RunArtifact(UUIDPkMixin, TimestampMixin, Base):
    """run 结果产物（方案 §4 P0-5）：清单/报告/大 JSON 一律落表并以 id 引用。

    纪律：进上下文的只留「引用行 + 预览」，正文在此表（`result.text` 只放面向人的摘要）。
    """

    __tablename__ = "run_artifacts"
    __table_args__ = (
        Index("ix_artifacts_run", "run_id"),
        Index("ix_artifacts_task", "task_id"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("runs.id", ondelete="CASCADE")
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, ForeignKey("tasks.id", ondelete="SET NULL")
    )
    step_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, ForeignKey("task_steps.id", ondelete="SET NULL")
    )
    kind: Mapped[str] = mapped_column(String(16), default="text")
    name: Mapped[str | None] = mapped_column(String(256))
    mime: Mapped[str | None] = mapped_column(String(128))
    size: Mapped[int] = mapped_column(Integer, default=0)
    storage: Mapped[str] = mapped_column(String(16), default="inline")
    payload: Mapped[dict | None] = mapped_column(JSONB)
    file_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, ForeignKey("files.id", ondelete="SET NULL")
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True)
