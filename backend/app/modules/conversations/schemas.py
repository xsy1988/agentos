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


class SendMessageOut(BaseModel):
    """发消息返回：run_id + 会话 id。前端转 SSE 订阅。"""

    conversation_id: UUID
    run_id: UUID
