"""m5a：memory_files + notifications 两表（记忆体系 + 通知中心）。"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB

from app.core.config import settings

revision = "b7e8f9a0c1d2"
down_revision = "a1d2c3f4e5b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_files",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("date", sa.Date(), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(dim=settings.embedding_dim), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("source_run_ids", JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memory_files")),
        sa.UniqueConstraint("kind", "date", name=op.f("uq_memory_files_kind_date")),
    )
    op.create_index("ix_memory_files_kind_date", "memory_files", ["kind", "date"])
    op.create_table(
        "notifications",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("url", sa.String(length=255), nullable=True),
        sa.Column("read", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_notifications_run_id_runs"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
    )
    op.create_index("ix_notifications_read_created", "notifications", ["read", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_notifications_read_created", table_name="notifications")
    op.drop_table("notifications")
    op.drop_index("ix_memory_files_kind_date", table_name="memory_files")
    op.drop_table("memory_files")
