"""notifications 表。"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import UUID, Base, UUIDPkMixin


class Notification(UUIDPkMixin, Base):
    __tablename__ = "notifications"

    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, ForeignKey("runs.id", ondelete="SET NULL")
    )
    kind: Mapped[str] = mapped_column(String(16))  # run_done / run_failed / alarm / budget / system
    title: Mapped[str] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(String(255))
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


Index("ix_notifications_read_created", Notification.read, Notification.created_at)
