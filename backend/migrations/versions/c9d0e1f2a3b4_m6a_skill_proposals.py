"""m6a：skill_proposals 表（技能沉淀草稿）。"""

import sqlalchemy as sa
from alembic import op

revision = "c9d0e1f2a3b4"
down_revision = "b7e8f9a0c1d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skill_proposals",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("draft_md", sa.Text(), nullable=False),
        sa.Column("similar_to_capability_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["similar_to_capability_id"], ["capabilities.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_skill_proposals_run_id", "skill_proposals", ["run_id"])
    op.create_index("ix_skill_proposals_status", "skill_proposals", ["status"])


def downgrade() -> None:
    op.drop_index("ix_skill_proposals_status", table_name="skill_proposals")
    op.drop_index("ix_skill_proposals_run_id", table_name="skill_proposals")
    op.drop_table("skill_proposals")
