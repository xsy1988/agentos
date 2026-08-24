"""memory_files 表：记忆文件（Markdown + 向量，可手改）。"""

from datetime import date

from pgvector.sqlalchemy import Vector
from sqlalchemy import Date, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import settings
from app.db.base import Base, TimestampMixin, UUIDPkMixin


class MemoryFile(UUIDPkMixin, TimestampMixin, Base):
    """kind=platform 平台记忆（长期不变的关键事实）/ daily 日期记忆（每日一条）。"""

    __tablename__ = "memory_files"
    __table_args__ = (UniqueConstraint("kind", "date"),)

    kind: Mapped[str] = mapped_column(String(16))  # platform / daily
    date: Mapped[date | None] = mapped_column(Date)  # daily 专用；platform 为空
    title: Mapped[str] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(settings.embedding_dim))
    token_count: Mapped[int] = mapped_column(Integer, default=0)  # 注入预算核算用
    source_run_ids: Mapped[list] = mapped_column(JSONB, default=list)  # 提炼来源可追溯


Index("ix_memory_files_kind_date", MemoryFile.kind, MemoryFile.date)
