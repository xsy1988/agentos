"""m4a：知识库四表 files / file_refs / kb_docs / kb_chunks（含 HNSW 余弦索引）

kb_chunks.embedding 与 capabilities 同维（1024，bge_m3_embed）；HNSW 索引
用 vector_cosine_ops 与 retriever 的 cosine_distance 检索配套。
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB

from app.core.config import settings

revision = "a1d2c3f4e5b6"
down_revision = "c4e1f0a3b7d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "files",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("path", sa.String(length=512), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("mime", sa.String(length=128), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_files")),
        sa.UniqueConstraint("sha256", name=op.f("uq_files_sha256")),
    )
    op.create_table(
        "file_refs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("file_id", sa.UUID(), nullable=False),
        sa.Column("ref_type", sa.String(length=16), nullable=False),
        sa.Column("ref_id", sa.UUID(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["file_id"], ["files.id"], name=op.f("fk_file_refs_file_id_files"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_file_refs")),
    )
    op.create_index(op.f("ix_file_refs_file_id"), "file_refs", ["file_id"])
    op.create_index("ix_file_refs_ref", "file_refs", ["ref_type", "ref_id"])
    op.create_table(
        "kb_docs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("folder_id", sa.UUID(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("source_file_id", sa.UUID(), nullable=True),
        sa.Column("source_type", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("embedding_model", sa.String(length=128), nullable=True),
        sa.Column("meta", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["folder_id"], ["kb_folders.id"], name=op.f("fk_kb_docs_folder_id_kb_folders"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_file_id"], ["files.id"], name=op.f("fk_kb_docs_source_file_id_files"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kb_docs")),
    )
    op.create_index(op.f("ix_kb_docs_status"), "kb_docs", ["status"])
    op.create_index("ix_kb_docs_folder_status", "kb_docs", ["folder_id", "status"])
    op.create_table(
        "kb_chunks",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("doc_id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("heading_path", sa.String(length=512), nullable=True),
        sa.Column("embedding", Vector(dim=settings.embedding_dim), nullable=True),
        sa.Column("embedding_model", sa.String(length=128), nullable=True),
        sa.Column("dim", sa.Integer(), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["doc_id"], ["kb_docs.id"], name=op.f("fk_kb_chunks_doc_id_kb_docs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kb_chunks")),
    )
    op.create_index(op.f("ix_kb_chunks_doc_id"), "kb_chunks", ["doc_id"])
    # HNSW 余弦索引：与 cosine_distance 检索配套（capabilities 侧量小暂不需要）
    op.execute(
        "CREATE INDEX ix_kb_chunks_embedding_hnsw ON kb_chunks "
        "USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_kb_chunks_doc_id"), table_name="kb_chunks")
    op.execute("DROP INDEX IF EXISTS ix_kb_chunks_embedding_hnsw")
    op.drop_table("kb_chunks")
    op.drop_index("ix_kb_docs_folder_status", table_name="kb_docs")
    op.drop_index(op.f("ix_kb_docs_status"), table_name="kb_docs")
    op.drop_table("kb_docs")
    op.drop_index("ix_file_refs_ref", table_name="file_refs")
    op.drop_index(op.f("ix_file_refs_file_id"), table_name="file_refs")
    op.drop_table("file_refs")
    op.drop_table("files")
