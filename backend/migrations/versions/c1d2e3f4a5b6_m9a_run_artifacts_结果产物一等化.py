"""m9a：run_artifacts 结果产物一等化（方案 §4 P0-5）。

问题：结果为单字段文本 `{"text": ...}`，清单/报告类长结果只能挤在 `result.text` 里，
进上下文后被 L1 压缩折成 `[:120]`——折叠即失真；run 也没有可复用的产物载体。

本迁移只建一张表 + 两个索引（不改既有列、不动历史数据）：
- 正文存 `payload`（inline）或 `file_id`（大文件走既有 files 表）；
- `kind` 区分 text/json/file/link，`storage` 区分 inline/pg/file；
- `idempotency_key` 唯一：重放/重试不会重复落同一产物。

存量 run 的 `result`（`{"text": ...}`）**不做批量改写**：读取侧
`timing.coalesce_result` 按 `run_result/v0` 解释（不可逆且无收益）。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c1d2e3f4a5b6"
down_revision = "a9b8c7d6e5f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "run_artifacts",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "step_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("task_steps.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(16), nullable=False, server_default="text"),
        sa.Column("name", sa.String(256), nullable=True),
        sa.Column("mime", sa.String(128), nullable=True),
        sa.Column("size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("storage", sa.String(16), nullable=False, server_default="inline"),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column(
            "file_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("files.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("idempotency_key", sa.String(128), nullable=True, unique=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_artifacts_run", "run_artifacts", ["run_id"])
    op.create_index("ix_artifacts_task", "run_artifacts", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_artifacts_task", table_name="run_artifacts")
    op.drop_index("ix_artifacts_run", table_name="run_artifacts")
    op.drop_table("run_artifacts")
