"""agents 表。"""

import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import UUID, Base, TimestampMixin, UUIDPkMixin


class Agent(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "agents"

    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="")
    avatar: Mapped[str | None] = mapped_column(String(255))
    # 人格三件套 + 补丁位（见数据库设计 §1.2）
    soul_md: Mapped[str] = mapped_column(Text, default="")
    identity_md: Mapped[str] = mapped_column(Text, default="")
    memory_md: Mapped[str] = mapped_column(Text, default="")
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    model_provider_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, ForeignKey("model_providers.id", ondelete="RESTRICT")
    )
    # 预算四闸中的三闸快照来源（第四闸为全局超时配置）
    tool_budget: Mapped[int] = mapped_column(Integer, default=8)
    max_iterations: Mapped[int] = mapped_column(Integer, default=25)
    max_tokens_per_run: Mapped[int | None] = mapped_column(Integer)
    timeout_seconds: Mapped[int | None] = mapped_column(Integer)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="enabled")
