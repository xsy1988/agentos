"""runs 请求/响应模型。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ConfirmIn(BaseModel):
    """确认卡片提交。

    - `approved` 继续执行 / `rejected` 拒绝计划（原有语义）；
    - 其它任意文本 = 支线子任务的用户答复（ADR-24，向 `ask_user` 的 interrupt 回注）；
    - `data`/`applied`（可选）= 侧边栏 plugin 前端的统一结构化回传（§3.5），
      与 answer 一并注入 interrupt，供 request_decision 恢复续跑。
    """

    answer: str = Field(default="", max_length=4000)
    # 结构化回传主体（array | object）：侧边栏选/删/改后的结果
    data: Any | None = None
    # 已落库的写入类 mcp 结果：[{capability, result}]
    applied: list[dict[str, Any]] | None = None


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
