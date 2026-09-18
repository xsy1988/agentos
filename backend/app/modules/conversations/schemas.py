"""conversations 请求/响应模型。"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ConversationCreateIn(BaseModel):
    agent_id: UUID | None = None  # 空则用默认 Agent
    title: str = Field(default="新会话", max_length=255)


class ConversationUpdateIn(BaseModel):
    """重命名 / 关闭会话（均可选字段，部分更新）。"""

    title: str | None = Field(default=None, min_length=1, max_length=255)
    status: Literal["active", "closed"] | None = None


class MessageIn(BaseModel):
    # 有附件时允许纯图输入（“看看这张图”）；限制拍板：单条消息 ≤5 个附件
    text: str = Field(default="", max_length=32000)
    attachment_ids: list[UUID] = Field(default_factory=list, max_length=5)
    # 对话内临时换模型（run 级覆盖）：空则用 Agent 绑定的模型
    model_provider_id: UUID | None = None
    # 图片外发涉密确认：带图消息且生效模型支持视觉时，首次未确认返回 428，
    # 前端弹窗确认后携 true 重发
    confirm_upload: bool = False
    # 用户对「这像是一个新主任务」提示选择「仍在本会话继续」时回传（ADR-27）
    force_current_task: bool = False
    # 幂等键（P1-9）：前端为一次「提交意图」生成，重试复用同一个键；
    # 同会话同键的重复提交返回首次的 run，不再新建
    client_message_id: str | None = Field(default=None, max_length=64)
    # 输入契约取值（P1-4）：{输入名: 取值}，由外部系统/前端表单提供；
    # 只接受 Worker 声明过的键会被采用，未声明的键忽略（值校验见 workers.inputs）
    inputs: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_non_empty(self) -> "MessageIn":
        if not self.text.strip() and not self.attachment_ids:
            raise ValueError("消息不能为空：需要文本或至少一个附件")
        return self


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    role: str
    content: dict
    run_id: UUID | None
    created_at: datetime


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    agent_id: UUID
    title: str
    status: str
    message_count: int
    last_message_at: datetime | None
    created_at: datetime


class SendMessageRunCreated(BaseModel):
    """正常路径：run 已创建并投递，前端转 SSE 订阅。"""

    kind: Literal["run_created"] = "run_created"
    conversation_id: UUID
    run_id: UUID


class TaskSwitchSuggestion(BaseModel):
    """检测到疑似新的主任务：只给建议，不落消息不建 run（ADR-27 软提示）。

    worker_name = data/workers 目录名（POST /tasks 直接复用）。
    """

    worker_name: str
    worker_display_name: str
    worker_icon: str | None = None
    confidence: float = 0.0
    reason: str = ""


class SendMessageTaskSwitch(BaseModel):
    kind: Literal["task_switch_suggested"] = "task_switch_suggested"
    conversation_id: UUID
    suggested_worker: TaskSwitchSuggestion
    # 原样回传用户的输入，前端「新开会话并发送」时直接复用，无需用户重打
    pending_text: str
    current_task_name: str


SendMessageOut = SendMessageRunCreated | SendMessageTaskSwitch
