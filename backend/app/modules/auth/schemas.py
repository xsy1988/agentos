"""auth 模块请求/响应模型。"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class UserInitIn(BaseModel):
    """首次初始化引导：创建唯一账号（已存在则 409）。"""

    username: str = Field(min_length=2, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=1, max_length=64)


class UserLoginIn(BaseModel):
    username: str
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    username: str
    display_name: str
    avatar: str | None
