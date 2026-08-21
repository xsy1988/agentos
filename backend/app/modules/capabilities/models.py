"""capabilities / capability_tools / capability_bindings 表。"""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import settings
from app.db.base import UUID, Base, TimestampMixin, UUIDPkMixin


class Capability(UUIDPkMixin, TimestampMixin, Base):
    """统一能力表：tool / skill / mcp / plugin 四类一表（数据库设计 §1.4）。"""

    __tablename__ = "capabilities"

    type: Mapped[str] = mapped_column(String(16))  # tool / skill / mcp / plugin
    category: Mapped[str] = mapped_column(String(16))  # external / internal / builtin
    name: Mapped[str] = mapped_column(String(128), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[str] = mapped_column(String(32), default="0.1.0")
    risk_level: Mapped[str] = mapped_column(String(16), default="read")  # read/write/dangerous
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    test_info: Mapped[list | None] = mapped_column(JSONB)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(settings.embedding_dim))
    embedding_model: Mapped[str | None] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    health_status: Mapped[str] = mapped_column(String(16), default="unknown")
    last_health_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CapabilityTool(Base):
    """MCP Server 内工具级开关——上下文膨胀的最后闸门。"""

    __tablename__ = "capability_tools"
    __table_args__ = (UniqueConstraint("capability_id", "tool_name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    capability_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("capabilities.id", ondelete="CASCADE"), index=True
    )
    tool_name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class CapabilityBinding(Base):
    """Agent↔能力绑定：pinned 常驻装配 / semantic 参与语义检索池。"""

    __tablename__ = "capability_bindings"
    __table_args__ = (UniqueConstraint("agent_id", "capability_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    capability_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("capabilities.id", ondelete="CASCADE"), index=True
    )
    mode: Mapped[str] = mapped_column(String(16), default="semantic")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
