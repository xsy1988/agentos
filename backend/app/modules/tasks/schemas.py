"""tasks 请求/响应模型（Worker 定义已文件化，只剩实例层）。"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# ---------- 任务实例（L2 实例层） ----------


class ConversationBrief(BaseModel):
    """任务看板卡片上的会话摘要。"""

    id: UUID
    title: str
    status: str
    message_count: int
    last_message_at: datetime | None


class TaskStepOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    seq: int
    name: str
    description: str
    kind: str
    status: str
    source: str
    resolution: dict | None
    run_id: UUID | None
    raised_at: datetime | None
    resolved_at: datetime | None
    updated_at: datetime


class TaskOut(BaseModel):
    id: UUID
    worker_name: str
    worker_display_name: str
    worker_version: str
    worker_icon: str | None
    agent_id: UUID
    title: str
    status: str
    progress_done: int
    progress_total: int
    progress_percent: int
    out_of_scope_count: int
    conversation: ConversationBrief | None
    # 待确认：本任务存在 paused_awaiting_confirm 的 run，或仍有 awaiting_user 的子任务
    awaiting_confirm: bool
    # awaiting_user 状态的子任务数（看板角标：还有几个支线等用户答复）
    awaiting_steps_count: int = 0
    active_run_id: UUID | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


class TaskDetailOut(TaskOut):
    steps: list[TaskStepOut] = Field(default_factory=list)
    run_ids: list[UUID] = Field(default_factory=list)


class WorkerGroupBrief(BaseModel):
    """看板分组头：来自文件注册中心的 Worker 概览（或已删除 Worker 的降级信息）。"""

    name: str
    display_name: str
    description: str = ""
    icon: str | None = None
    enabled: bool = True
    active_version: str | None = None


class TaskGroupOut(BaseModel):
    """看板分组：一个 Worker + 组内任务实例 + 组级汇总（看板头部展示用）。"""

    worker: WorkerGroupBrief
    # 汇总按组内**全部**实例计算（tasks 字段受 limit_per_group 截断）
    task_count: int = 0
    active_count: int = 0
    awaiting_confirm_count: int = 0
    progress_percent: int = 0
    tasks: list[TaskOut] = Field(default_factory=list)


class TaskCreateIn(BaseModel):
    """新建主任务实例：后端一把创建 会话 + 任务实例 + 步骤骨架（+ 首条 run）。

    worker_name = data/workers 目录名；创建时锁定其生效版本。
    """

    worker_name: str = Field(min_length=1, max_length=128)
    title: str | None = Field(default=None, max_length=255)
    agent_id: UUID | None = None
    text: str = Field(default="", max_length=32000)
    model_provider_id: UUID | None = None
    # 幂等键（P1-9）：「新开会话并发送」连点两次不会建出两个主任务
    client_message_id: str | None = Field(default=None, max_length=64)


class TaskCreateOut(BaseModel):
    task: TaskOut
    conversation_id: UUID
    run_id: UUID | None


class TaskUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=255)
    status: Literal["active", "done", "cancelled"] | None = None


class StepCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=4000)
    kind: Literal["main", "branch"] = "branch"


class StepUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["pending", "doing", "done", "skipped", "blocked", "awaiting_user"] | None = None
    resolution: dict | None = None


class StepConvergeIn(BaseModel):
    """受阻支线的前台收敛动作（P1-6）。"""

    model_config = ConfigDict(extra="forbid")

    action: Literal["close", "requeue", "escalate"]
    # 自由文本只做人话补充：收敛原因恒为枚举 manual，不在文本里写理由（P1-6）
    detail: str | None = Field(default=None, max_length=500)
