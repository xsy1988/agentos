"""tasks 请求/响应模型。"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------- 任务模板（L1 定义层） ----------


class StepTemplateIn(BaseModel):
    """子任务模板条目（PUT /task-types/{id}/steps 整表替换用）。"""

    name: str = Field(min_length=1, max_length=255)
    # L1 简要描述
    description: str = Field(default="", max_length=4000)
    # L2 playbook 正文：推进到该子任务时载入
    playbook: str = Field(default="", max_length=50000)
    # L3 引用资源：子 WORKER.md / 卡片模板 / 数据契约等（按需读取）
    references: list[dict] | None = None
    kind: Literal["main", "branch"] = "main"
    optional: bool = False
    capability_hint: list[str] | None = None


class TaskTypeCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    # L1 简要描述：一句话讲清「什么场景用它」
    description: str = Field(default="", max_length=4000)
    # L2 playbook 正文：Worker 激活时随任务卡注入永不压缩区
    playbook: str = Field(default="", max_length=50000)
    # L3 引用资源（按需读取）
    references: list[dict] | None = None
    icon: str | None = Field(default=None, max_length=32)
    color: str | None = Field(default=None, max_length=16)
    sort_order: int = 0
    default_agent_id: UUID | None = None
    enabled: bool = True
    # 建模板时一并提交步骤模板（可选，省一次请求）
    steps: list[StepTemplateIn] = Field(default_factory=list, max_length=50)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("主任务名称不能为空")
        return v


class TaskTypeUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=4000)
    playbook: str | None = Field(default=None, max_length=50000)
    references: list[dict] | None = None
    icon: str | None = Field(default=None, max_length=32)
    color: str | None = Field(default=None, max_length=16)
    sort_order: int | None = None
    default_agent_id: UUID | None = None
    enabled: bool | None = None


class StepTemplateReplaceIn(BaseModel):
    """整表替换：模板量小，避免逐条 CRUD 的排序竞态。"""

    steps: list[StepTemplateIn] = Field(default_factory=list, max_length=50)


class StepTemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    seq: int
    name: str
    description: str
    playbook: str
    references: list[dict] | None
    kind: str
    optional: bool
    capability_hint: list[str] | None


class TaskTypeCapabilityOut(BaseModel):
    """归属关系条目（含能力摘要，便于前端直接渲染标签）。"""

    capability_id: UUID
    name: str
    type: str
    risk_level: str
    enabled: bool


class TaskTypeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str
    playbook: str
    references: list[dict] | None
    kind: str
    icon: str | None
    color: str | None
    sort_order: int
    default_agent_id: UUID | None
    enabled: bool
    created_at: datetime
    updated_at: datetime
    # 列表视图一并返回，避免前端二次请求
    steps: list[StepTemplateOut] = Field(default_factory=list)
    capability_count: int = 0
    task_count: int = 0


class CapabilityBindIn(BaseModel):
    capability_ids: list[UUID] = Field(min_length=1, max_length=200)


class WorkerFileOut(BaseModel):
    """WORKER.md 文件内容（主任务或子任务，含相对路径供展示）。"""

    path: str
    content: str


class WorkerFileIn(BaseModel):
    """编辑保存：整文件内容（frontmatter + playbook 正文）。"""

    content: str = Field(min_length=1, max_length=200_000)


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
    task_type_id: UUID
    task_type_name: str
    task_type_icon: str | None
    task_type_color: str | None
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


class TaskGroupOut(BaseModel):
    """看板分组：一个主任务模板 + 组内任务实例 + 组级汇总（看板头部展示用）。"""

    task_type: TaskTypeOut
    # 汇总按组内**全部**实例计算（tasks 字段受 limit_per_group 截断）
    task_count: int = 0
    active_count: int = 0
    awaiting_confirm_count: int = 0
    progress_percent: int = 0
    tasks: list[TaskOut] = Field(default_factory=list)


class TaskCreateIn(BaseModel):
    """新建主任务实例：后端一把创建 会话 + 任务实例 + 步骤骨架（+ 首条 run）。"""

    task_type_id: UUID
    title: str | None = Field(default=None, max_length=255)
    agent_id: UUID | None = None
    text: str = Field(default="", max_length=32000)
    model_provider_id: UUID | None = None


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

    status: Literal["pending", "doing", "done", "skipped", "blocked", "awaiting_user"] | None = (
        None
    )
    resolution: dict | None = None
