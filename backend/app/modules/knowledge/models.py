"""知识库表：kb_folders 目录树 + kb_docs 文档元数据 + kb_chunks 文本切片（向量）。"""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import settings
from app.db.base import UUID, Base, TimestampMixin, UUIDPkMixin


class KbFolder(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "kb_folders"

    name: Mapped[str] = mapped_column(String(128))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, ForeignKey("kb_folders.id", ondelete="RESTRICT")
    )
    # 物化路径，如 /产品知识/硬件规格；检索按前缀圈定目录范围
    path: Mapped[str] = mapped_column(String(512), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class KbDoc(UUIDPkMixin, TimestampMixin, Base):
    """知识文档：管道状态机 uploaded → parsing → chunking → embedding → ready | failed。"""

    __tablename__ = "kb_docs"

    folder_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("kb_folders.id", ondelete="RESTRICT")
    )
    title: Mapped[str] = mapped_column(String(255))
    source_file_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, ForeignKey("files.id", ondelete="SET NULL")
    )
    source_type: Mapped[str] = mapped_column(String(16))  # pdf / word / md / txt / url
    status: Mapped[str] = mapped_column(String(16), default="uploaded", index=True)
    error: Mapped[str | None] = mapped_column(Text)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    embedding_model: Mapped[str | None] = mapped_column(String(128))
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)


Index("ix_kb_docs_folder_status", KbDoc.folder_id, KbDoc.status)


class KbChunk(UUIDPkMixin, Base):
    """文本切片：512±128 token，带标题路径元数据与向量。"""

    __tablename__ = "kb_chunks"

    doc_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("kb_docs.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    heading_path: Mapped[str | None] = mapped_column(String(512))
    embedding: Mapped[list[float] | None] = mapped_column(Vector(settings.embedding_dim))
    embedding_model: Mapped[str | None] = mapped_column(String(128))
    dim: Mapped[int | None] = mapped_column(Integer)  # 换模型重建时校验用
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# 向量检索索引：与 m4a 迁移中的原生 DDL 同名同义，声明在 metadata 里是为了让
# autogenerate 不会把它当成"多余的索引"删掉（HNSW 无法用普通 Column index=True 表达）
Index(
    "ix_kb_chunks_embedding_hnsw",
    KbChunk.embedding,
    postgresql_using="hnsw",
    postgresql_ops={"embedding": "vector_cosine_ops"},
)
