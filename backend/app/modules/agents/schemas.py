"""agents 模块请求/响应模型。"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AgentBase(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = ""
    avatar: str | None = None
    soul_md: str = ""
    identity_md: str = ""
    memory_md: str = ""
    system_prompt: str = ""
    model_provider_id: UUID | None = None
    tool_budget: int = Field(default=8, ge=1, le=64)
    max_iterations: int = Field(default=25, ge=1, le=200)
    max_tokens_per_run: int | None = Field(default=None, ge=1000)
    timeout_seconds: int | None = Field(default=None, ge=10)


class AgentCreateIn(AgentBase):
    pass


class AgentUpdateIn(BaseModel):
    """全部可选；未传字段不动。"""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = None
    avatar: str | None = None
    soul_md: str | None = None
    identity_md: str | None = None
    memory_md: str | None = None
    system_prompt: str | None = None
    model_provider_id: UUID | None = None
    tool_budget: int | None = Field(default=None, ge=1, le=64)
    max_iterations: int | None = Field(default=None, ge=1, le=200)
    max_tokens_per_run: int | None = Field(default=None, ge=1000)
    timeout_seconds: int | None = Field(default=None, ge=10)
    status: str | None = Field(default=None, pattern=r"^(enabled|disabled)$")


class AgentOut(AgentBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    is_default: bool
    status: str
    created_at: datetime
    updated_at: datetime
