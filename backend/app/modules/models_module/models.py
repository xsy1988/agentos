"""model_providers 表。"""

from sqlalchemy import LargeBinary, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPkMixin


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
