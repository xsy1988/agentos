"""m9a：run 时长账本与真全局超时（方案 §4 P0-1）。

问题：`runs.started_at` 从未被写入（无时长账本）；全局超时按"执行段"重取
`timeout_seconds`，每次 interrupt/resume 都重置为满额，导致实际存活时间远超预算
（实测 run 存活 1401s、最后一段恰 600.004s、result 为 NULL）。

本迁移只加列（不改状态机）：
- `deadline_at`：首次进入 running 时一次写入的绝对截止时间，之后仅因暂停顺延；
- `active_ms`：执行段时长累计（不含暂停/等待）；
- `paused_at`：暂停起点，作为恢复时顺延截止点的依据。

存量数据：`started_at`/`deadline_at` 保持 NULL（**不做估算回填**，读取侧按
`coalesce(deadline_at, started_at + timeout)` 兜底）；`active_ms` 置 0。
"""

import sqlalchemy as sa
from alembic import op

revision = "a9b8c7d6e5f4"
down_revision = "g8d9e0f1a2b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("runs", sa.Column("active_ms", sa.Integer(), nullable=True))
    op.add_column("runs", sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True))
    # 存量 run 的执行账本从 0 起算（无历史依据，不臆造数值）
    op.execute(sa.text("UPDATE runs SET active_ms = 0 WHERE active_ms IS NULL"))
    op.create_index("ix_runs_deadline_at", "runs", ["deadline_at"])


def downgrade() -> None:
    op.drop_index("ix_runs_deadline_at", table_name="runs")
    op.drop_column("runs", "paused_at")
    op.drop_column("runs", "active_ms")
    op.drop_column("runs", "deadline_at")
