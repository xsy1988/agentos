"""await_broker 表（方案 §4 P0-4）：平台持有的外部等待。

一行 = 一次外部派发请求的等待契约。生命周期：
waiting → granted（外部回调成功）/ expired（超时巡检）/ cancelled（run 结束或人工撤销）。
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import UUID, Base, TimestampMixin, UUIDPkMixin

# 等待状态机：waiting → granted | expired | cancelled（后三者终态）
AWAIT_STATUSES = ("waiting", "granted", "expired", "cancelled")
AWAIT_TERMINAL_STATUSES = ("granted", "expired", "cancelled")


class AwaitBroker(UUIDPkMixin, TimestampMixin, Base):
    """外部等待凭证。callback_token 为回调侧专用凭据（与 X-API-Key 双重鉴权）。"""

    __tablename__ = "await_broker"
    __table_args__ = (
        # 幂等：同 run 同工具同参数视为同一笔外部请求，第二次起复用既有等待
        UniqueConstraint("run_id", "tool_name", "idempotency_key", name="uq_await_active"),
        Index("ix_await_runs_status", "run_id", "status"),
        # 部分索引：巡检只关心 waiting 行
        Index(
            "ix_await_deadline",
            "deadline_at",
            postgresql_where=text("status = 'waiting'"),
        ),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey("runs.id", ondelete="CASCADE"))
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, ForeignKey("conversations.id", ondelete="SET NULL")
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, ForeignKey("tasks.id", ondelete="SET NULL")
    )
    step_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, ForeignKey("task_steps.id", ondelete="SET NULL")
    )
    capability_id: Mapped[str | None] = mapped_column(String(128))
    tool_name: Mapped[str] = mapped_column(String(128))
    # 参数指纹（sha256 前 32 位）：模型重放同一派发时不重复出网
    idempotency_key: Mapped[str] = mapped_column(String(128))
    callback_token: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(
        String(16), default="waiting", server_default=text("'waiting'")
    )
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # 出网请求体 / 外部回调载荷
    payload_in: Mapped[dict | None] = mapped_column(JSONB)
    payload_out: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[dict | None] = mapped_column(JSONB)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    waited_ms: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
