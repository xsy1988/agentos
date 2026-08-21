"""model_providers / model_usage_daily 表。"""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import UUID, Base, TimestampMixin, UUIDPkMixin


class ModelProvider(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "model_providers"

    kind: Mapped[str] = mapped_column(String(16))  # llm / embedding
    name: Mapped[str] = mapped_column(String(128))
    impl: Mapped[str] = mapped_column(String(32))  # openai_compatible / anthropic / ollama
    base_url: Mapped[str] = mapped_column(String(255))
    api_key_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)  # Fernet，可空（本地模型）
    model_name: Mapped[str] = mapped_column(String(128))
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    limits: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="enabled")


class ModelUsageDaily(Base):
    """模型用量记账（数据库设计 §2.11）：分钟级 upsert 累加，MeteringHook 写入。"""

    __tablename__ = "model_usage_daily"
    __table_args__ = (UniqueConstraint("provider_id", "date"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    provider_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("model_providers.id", ondelete="CASCADE"), index=True
    )
    date: Mapped[date] = mapped_column(Date)
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    total_tokens: Mapped[int] = mapped_column(BigInteger, default=0)  # input+output，钩子直写
    run_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
