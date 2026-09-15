"""tasks 模块：ORM 模型。

四类实体对应设计文档的四层架构：
- L1 定义层：task_types（主任务模板，人工维护）+ task_type_steps（子任务模板）
- L2 实例层：tasks（主任务实例）+ task_steps（子任务实例）
- L3 归属层：task_type_capabilities（能力↔主任务）

`tasks.conversation_id` 唯一：一个会话承载一个主任务实例（timer 无会话 run 允许 NULL）。
模板的内建「通用任务」（kind=common）承载未归类能力与历史会话，保证旧行为不回归。
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPkMixin

UUID = PGUUID(as_uuid=True)

# 主任务模板类型：business=业务主任务；common=通用任务集（全局唯一，不可删除）
TASK_TYPE_KINDS = ("business", "common")
# 主任务实例状态
TASK_STATUSES = ("active", "done", "failed", "cancelled")
# 子任务种类：main=主线（模板必现）；branch=支线（按需触发）
STEP_KINDS = ("main", "branch")
# 子任务状态：awaiting_user=已向用户提问待答复
STEP_STATUSES = ("pending", "doing", "done", "skipped", "blocked", "awaiting_user")
# 子任务来源：template=模板实例化；planner=规划器新增；agent_raised=模型主动发起；user=人工添加
STEP_SOURCES = ("template", "planner", "agent_raised", "user")

# 内建通用任务集名称（seed 幂等键）
COMMON_TASK_TYPE_NAME = "通用任务"


class TaskType(UUIDPkMixin, TimestampMixin, Base):
    """主任务定义（任务模板）——人工维护，LLM 不参与创建。"""

    __tablename__ = "task_types"

    name: Mapped[str] = mapped_column(String(128), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(16), default="business")
    icon: Mapped[str | None] = mapped_column(String(32))
    color: Mapped[str | None] = mapped_column(String(16))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    default_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, ForeignKey("agents.id", ondelete="SET NULL")
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class TaskTypeStep(UUIDPkMixin, TimestampMixin, Base):
    """子任务模板：实例化时按 seq 生成 tasks 的子任务骨架。"""

    __tablename__ = "task_type_steps"
    __table_args__ = (Index("ix_task_type_steps_type_seq", "task_type_id", "seq"),)

    task_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("task_types.id", ondelete="CASCADE")
    )
    seq: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(16), default="main")
    optional: Mapped[bool] = mapped_column(Boolean, default=False)
    # 建议能力名列表，供域内检索偏置（可选，不强制）
    capability_hint: Mapped[list | None] = mapped_column(JSONB)


class Task(UUIDPkMixin, TimestampMixin, Base):
    """主任务实例：一次主任务执行 = 一个会话。"""

    __tablename__ = "tasks"
    __table_args__ = (Index("ix_tasks_type_status", "task_type_id", "status"),)

    task_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("task_types.id", ondelete="RESTRICT")
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("agents.id", ondelete="RESTRICT")
    )
    # 唯一：一个会话一个主任务；timer 无会话 run 的任务为 NULL
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, ForeignKey("conversations.id", ondelete="CASCADE"), unique=True
    )
    title: Mapped[str] = mapped_column(String(255), default="新任务")
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    # 反范式进度，由 task_steps 状态重算（确定性，不由 LLM 估算）
    progress_done: Mapped[int] = mapped_column(Integer, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, default=0)
    # 越界请求记录：[{text, at}]——用户选择「仍在本会话继续」时落库
    out_of_scope: Mapped[list] = mapped_column(JSONB, default=list)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TaskStep(UUIDPkMixin, TimestampMixin, Base):
    """子任务实例（任务步骤）：主线与支线同表，靠 kind 区分。"""

    __tablename__ = "task_steps"
    __table_args__ = (Index("ix_task_steps_task_seq", "task_id", "seq"),)

    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("tasks.id", ondelete="CASCADE")
    )
    seq: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(16), default="main")
    status: Mapped[str] = mapped_column(String(16), default="pending")
    source: Mapped[str] = mapped_column(String(16), default="template")
    # 由模板实例化时回指的模板行（模型新增/人工添加为 NULL）
    template_step_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, ForeignKey("task_type_steps.id", ondelete="SET NULL")
    )
    # 触发/处理该步骤的 run（重放与溯源）
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, ForeignKey("runs.id", ondelete="SET NULL")
    )
    # 支线子任务的用户答复：{answer, at}
    resolution: Mapped[dict | None] = mapped_column(JSONB)
    raised_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TaskTypeCapability(Base):
    """能力↔主任务归属（多对多）。应用层保证每条能力至少归属「通用任务」。"""

    __tablename__ = "task_type_capabilities"
    __table_args__ = (UniqueConstraint("task_type_id", "capability_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    task_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("task_types.id", ondelete="CASCADE"), index=True
    )
    capability_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("capabilities.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
