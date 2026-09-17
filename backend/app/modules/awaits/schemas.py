"""awaits 请求/响应模型（方案 §4 P0-4）。"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class AwaitOut(BaseModel):
    await_id: str
    run_id: str
    tool: str
    status: str
    deadline_at: datetime | None = None
    waited_ms: int = 0
    attempts: int = 0
    notified_at: datetime | None = None
    resolved_at: datetime | None = None
    error: dict[str, Any] | None = None


class AwaitResolveIn(BaseModel):
    """外部服务回调载荷。callback_token 必需（与 X-API-Key 双重鉴权）。"""

    callback_token: str
    # 幂等键可选：外部服务重试时带上首次登记时的 key，平台据此拒绝串号回调
    idempotency_key: str | None = None
    payload: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


class AwaitResolveOut(BaseModel):
    await_id: str
    run_id: str
    status: str
    # resumed=False 表示幂等重放/已被别的路径落定：平台只回既有状态，不重复唤醒
    resumed: bool = False
    waited_ms: int = 0


class AwaitCancelOut(BaseModel):
    await_id: str
    run_id: str
    status: str
    cancelled: bool = False


class AwaitListOut(BaseModel):
    items: list[AwaitOut] = Field(default_factory=list)
