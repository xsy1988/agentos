"""tasks 模块：ORM 模型。

Worker（主任务模板）已完全文件化（app/modules/workers/registry.py，
data/workers/<name>/<version>/WORKER.md 文件包），不再落库；本模块只剩实例层：
- tasks（主任务实例）：任务创建时绑定 worker_name + worker_version（锁定当时生效版本）
- task_steps（子任务实例）：worker_step_ref 回指 sub_workers/<ref>/WORKER.md

`tasks.conversation_id` 唯一：一个会话承载一个主任务实例（timer 无会话 run 允许 NULL）。
未选择 Worker 的会话落内建 COMMON_WORKER（"__common__"，无步骤骨架）。
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPkMixin

UUID = PGUUID(as_uuid=True)

# 主任务实例状态
TASK_STATUSES = ("active", "done", "failed", "cancelled")
# 子任务种类：main=主线（模板必现）；branch=支线（按需触发）
STEP_KINDS = ("main", "branch")
# 子任务状态：awaiting_user=已向用户提问待答复
STEP_STATUSES = ("pending", "doing", "done", "skipped", "blocked", "awaiting_user")
# 子任务来源：template=模板实例化；planner=规划器新增；agent_raised=模型主动发起；user=人工添加
STEP_SOURCES = ("template", "planner", "agent_raised", "user")
# 支线受阻原因（P1-6）：枚举化，禁止在自由文本里写收敛理由
# run_ended=run 已结束不会再有人答复；deadline_exceeded=超时；
# user_cancelled=用户取消/中止；manual=人工收敛
STEP_BLOCK_REASONS = ("run_ended", "deadline_exceeded", "user_cancelled", "manual")
# 前台收敛动作（P1-6）：close=关闭支线（不再需要）；requeue=重新排队（回 pending 待重跑）；
# escalate=转人工（保持受阻，标记已人工确认接手）
STEP_CONVERGE_ACTIONS = ("close", "requeue", "escalate")


class Task(UUIDPkMixin, TimestampMixin, Base):
    """主任务实例：一次主任务执行 = 一个会话。

    worker_name/worker_version 在创建时锁定（绑定当时生效版本）：
    Worker 构建新版本后，新任务用新版本，进行中任务继续用旧版本文件。
    """

    __tablename__ = "tasks"
    __table_args__ = (Index("ix_tasks_worker_status", "worker_name", "status"),)

    # Worker 标识 = data/workers/ 目录名；内建兜底为 COMMON_WORKER（"__common__"）
    worker_name: Mapped[str] = mapped_column(String(128), default="__common__")
    # 任务创建时锁定的版本（"v1"…；通用任务为空串）
    worker_version: Mapped[str] = mapped_column(String(16), default="")
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey("agents.id", ondelete="RESTRICT"))
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
    """子任务实例（任务步骤）：主线与支线同表，靠 kind 区分。

    worker_step_ref 回指 Worker 文件包里的子任务文件夹名
    （<worker>/sub_workers/<ref>/WORKER.md）；模型新增/人工添加的步骤为 NULL。
    """

    __tablename__ = "task_steps"
    __table_args__ = (Index("ix_task_steps_task_seq", "task_id", "seq"),)

    task_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey("tasks.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(16), default="main")
    status: Mapped[str] = mapped_column(String(16), default="pending")
    source: Mapped[str] = mapped_column(String(16), default="template")
    # 模板实例化时回指的 sub_workers 文件夹名（模型新增/人工添加为 NULL）
    worker_step_ref: Mapped[str | None] = mapped_column(String(255))
    # 触发/处理该步骤的 run（重放与溯源）
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, ForeignKey("runs.id", ondelete="SET NULL")
    )
    # 支线子任务的用户答复：{answer, at}
    resolution: Mapped[dict | None] = mapped_column(JSONB)
    raised_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
