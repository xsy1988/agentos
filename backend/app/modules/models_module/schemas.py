"""model_providers 请求/响应模型。密钥只进不出。"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelProviderBase(BaseModel):
    kind: str = Field(pattern=r"^(llm|embedding)$")
    name: str = Field(min_length=1, max_length=128)
    impl: str = Field(pattern=r"^(openai_compatible|anthropic|ollama)$")
    base_url: str = Field(min_length=1, max_length=255)
    model_name: str = Field(min_length=1, max_length=128)
    params: dict = Field(default_factory=dict)
    limits: dict = Field(default_factory=dict)


class ModelProviderCreateIn(ModelProviderBase):
    api_key: str | None = Field(default=None, max_length=512)  # 明文只在此出现一次


class ModelProviderUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=128)
    base_url: str | None = Field(default=None, min_length=1, max_length=255)
    model_name: str | None = Field(default=None, min_length=1, max_length=128)
    api_key: str | None = Field(default=None, max_length=512)
    params: dict | None = None
    limits: dict | None = None
    status: str | None = Field(default=None, pattern=r"^(enabled|disabled)$")


class ModelProviderOut(ModelProviderBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: str
    has_api_key: bool  # 只报有无，不回内容
    created_at: datetime
    updated_at: datetime
