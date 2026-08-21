"""timers / alarms 表（定时任务与闹钟分离，见数据库设计 §1.8/§1.9）。"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import UUID, Base, TimestampMixin, UUIDPkMixin


class Timer(UUIDPkMixin, TimestampMixin, Base):
    """定时任务：到点由指定 Agent 产生 run（执行业务）。"""

    __tablename__ = "timers"

    name: Mapped[str] = mapped_column(String(128))
    cron_expr: Mapped[str] = mapped_column(String(64))
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("agents.id", ondelete="RESTRICT"), index=True
    )
    input_template: Mapped[dict] = mapped_column(JSONB, default=dict)
    next_fire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_fire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="active")  # active / paused


class Alarm(UUIDPkMixin, Base):
    """闹钟：纯提醒，不产生 run。"""

    __tablename__ = "alarms"

    content: Mapped[str] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(String(255))
    fire_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(16), default="waiting")  # waiting/fired/cancelled
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
