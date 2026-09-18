"""m9b：await_broker 平台持有外部等待（方案 §4 P0-4）。

问题：分钟级外部流程（如采购报价）只能靠模型反复调用 `*_status` 轮询表达，等待期间
占着 run 的执行段（iteration/时长预算被轮询吃掉），且无法重启恢复、无幂等保证。

本迁移只建一张表 + 三个索引（不改既有列、不动历史数据）：
- 一行 = 一次外部派发的等待契约：`waiting → granted | expired | cancelled`；
- `(run_id, tool_name, idempotency_key)` 复合唯一：同 run 同工具同参数视为同一笔
  外部请求（**不做全局唯一**——不同 run 的相同参数是两笔合法请求）；
- `callback_token`：回调侧专属凭据（HMAC 派生，与静态 X-API-Key 双重鉴权）；
- 部分索引 `ix_await_deadline` 只覆盖 `waiting` 行，供超时巡检走索引。

存量数据：无需回填（等待表此前不存在）。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b2c3d4e5f6a7"
down_revision = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "await_broker",
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
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="SET NULL"),
            nullable=True,
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
        sa.Column("capability_id", sa.String(128), nullable=True),
        sa.Column("tool_name", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("callback_token", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="waiting"),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_in", postgresql.JSONB(), nullable=True),
        sa.Column("payload_out", postgresql.JSONB(), nullable=True),
        sa.Column("error", postgresql.JSONB(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("waited_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("run_id", "tool_name", "idempotency_key", name="uq_await_active"),
    )
    op.create_index("ix_await_runs_status", "await_broker", ["run_id", "status"])
    op.create_index(
        "ix_await_deadline",
        "await_broker",
        ["deadline_at"],
        postgresql_where=sa.text("status = 'waiting'"),
    )


def downgrade() -> None:
    op.drop_index("ix_await_deadline", table_name="await_broker")
    op.drop_index("ix_await_runs_status", table_name="await_broker")
    op.drop_table("await_broker")
