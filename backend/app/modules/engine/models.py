"""inbox_events（引擎事件队列）+ plans（计划外置）表。

inbox：Engine 的统一消费入口，分叉/恢复/重放/二期拆进程全部是队列操作，图本体不感知。
plans：一 run 一活动计划，State 只存 plan_ref——计划在库里，不在上下文。
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPkMixin

UUID = PGUUID(as_uuid=True)

# 引擎 inbox 事件类型 —— 与本文件之外的唯一真源同步：runtime.py `_handle` 实现。
# 只登记**已实现**的类型（P0-6：不得保留"声明了但没实现"的条目）：
#   - user_input：API 建 run 后投递（conversations/scheduler）
#   - abort：用户取消（runs/conversations router）
#   - confirmation：request_decision 答复（runs router）
# 两类**不经本队列**：
#   - capability_changed：走 PG NOTIFY 频道（capabilities/service.py:650 → mcp_client.py:509），
#     不落 inbox_events [实测从未落库]
#   - resume：恢复由 confirmation 载荷驱动（runtime.py `_resume_run`），无需独立类型
INBOX_EVENT_TYPES = ("user_input", "abort", "confirmation")


class InboxEvent(Base):
    """引擎事件队列：status new → consumed；配合 LISTEN/NOTIFY 即时唤醒。"""

    __tablename__ = "inbox_events"
    __table_args__ = (Index("ix_inbox_status_created", "status", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(32))
    target_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID, index=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="new")
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Plan(UUIDPkMixin, TimestampMixin, Base):
    """计划外置：items = [{seq, text, status: pending|doing|done|skipped}]。"""

    __tablename__ = "plans"

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("runs.id", ondelete="CASCADE"), unique=True
    )
    items: Mapped[list] = mapped_column(JSONB, default=list)
