"""m7b：Worker/WORKER.md——主任务/子任务模板扩 L2 playbook + L3 references。

决策3：任务改判为 Worker（任务型 Agent），WORKER.md 三级逐层披露——
L1（name+description，已有）/ L2（playbook 正文，本次新增）/ L3（references 引用，本次新增）。
仅加列、不动主干：旧模板 playbook 为空串、references 为 NULL，任务卡注入与看板行为零回归。

playbook 为 NOT NULL（与 ORM `default=""` 对齐）：ADD COLUMN 到非空表需先带
server_default 回填既有行，落定后撤掉 DB 层默认（保持默认值只在 ORM 侧，避免双源）。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f2b3c4d5e6a7"
down_revision = "e1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---------- task_types（主任务 Worker）----------
    op.add_column(
        "task_types",
        sa.Column("playbook", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "task_types",
        sa.Column("references", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    # ---------- task_type_steps（子任务 Worker）----------
    op.add_column(
        "task_type_steps",
        sa.Column("playbook", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "task_type_steps",
        sa.Column("references", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    # 回填完成后撤掉 server_default（默认值只保留在 ORM 侧）
    op.alter_column("task_types", "playbook", server_default=None)
    op.alter_column("task_type_steps", "playbook", server_default=None)


def downgrade() -> None:
    op.drop_column("task_type_steps", "references")
    op.drop_column("task_type_steps", "playbook")
    op.drop_column("task_types", "references")
    op.drop_column("task_types", "playbook")
