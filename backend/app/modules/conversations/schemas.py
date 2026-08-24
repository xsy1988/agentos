"""conversations 请求/响应模型。"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ConversationCreateIn(BaseModel):
    agent_id: UUID | None = None  # 空则用默认 Agent
    title: str = Field(default="新会话", max_length=255)


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=32000)
    # 对话内临时换模型（run 级覆盖）：空则用 Agent 绑定的模型
    model_provider_id: UUID | None = None


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
