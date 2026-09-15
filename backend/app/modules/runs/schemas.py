"""runs 请求/响应模型。"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ConfirmIn(BaseModel):
    """确认卡片提交。

    - `approved` 继续执行 / `rejected` 拒绝计划（原有语义）；
    - 其它任意文本 = 支线子任务的用户答复（ADR-24，向 `ask_user` 的 interrupt 回注）。
    """

    answer: str = Field(min_length=1, max_length=4000)


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    conversation_id: UUID | None
    agent_id: UUID
    trigger: str
    status: str
    input: dict
    result: dict | None
    error: dict | None
    budget: dict
    budget_used: dict
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


class RunEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    run_id: UUID
    seq: int
    event_type: str
    payload: dict
    created_at: datetime
