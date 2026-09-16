"""m7a：任务架构——主任务/子任务（task_types / task_type_steps / tasks / task_steps）。

四层架构的落库（设计方案 ADR-23~28）：
- L1 定义层 task_types + task_type_steps（人工维护的主任务模板与子任务模板）
- L2 实例层 tasks + task_steps（一个会话承载一个主任务实例）
- L3 归属层 task_type_capabilities（能力↔主任务，软约束召回用）

回填（保证旧行为零回归）：
1. seed 内建「通用任务」（kind=common，不可删除）
2. 全部既有 capability 归属「通用任务」→ 等价于原本的全局可用
3. 每个既有 conversation 建一条 tasks（通用任务）→ 旧会话仍出现在任务看板
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e1a2b3c4d5e6"
down_revision = "c9d0e1f2a3b4"
branch_labels = None
depends_on = None

COMMON_TASK_TYPE_NAME = "通用任务"


def upgrade() -> None:
    # ---------- L1 定义层 ----------
    op.create_table(
        "task_types",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("icon", sa.String(length=32), nullable=True),
        sa.Column("color", sa.String(length=16), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("default_agent_id", sa.UUID(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["default_agent_id"],
            ["agents.id"],
            name="fk_task_types_default_agent_id_agents",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_task_types"),
        sa.UniqueConstraint("name", name="uq_task_types_name"),
    )

    op.create_table(
        "task_type_steps",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("task_type_id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("optional", sa.Boolean(), nullable=False),
        sa.Column("capability_hint", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["task_type_id"],
            ["task_types.id"],
            name="fk_task_type_steps_task_type_id_task_types",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_task_type_steps"),
    )
    op.create_index("ix_task_type_steps_type_seq", "task_type_steps", ["task_type_id", "seq"])

    # ---------- L2 实例层 ----------
    op.create_table(
        "tasks",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("task_type_id", sa.UUID(), nullable=False),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("conversation_id", sa.UUID(), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("progress_done", sa.Integer(), nullable=False),
        sa.Column("progress_total", sa.Integer(), nullable=False),
        sa.Column("out_of_scope", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["task_type_id"],
            ["task_types.id"],
            name="fk_tasks_task_type_id_task_types",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"], name="fk_tasks_agent_id_agents", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name="fk_tasks_conversation_id_conversations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tasks"),
        sa.UniqueConstraint("conversation_id", name="uq_tasks_conversation_id"),
    )
    op.create_index("ix_tasks_status", "tasks", ["status"])
    op.create_index("ix_tasks_type_status", "tasks", ["task_type_id", "status"])

    op.create_table(
        "task_steps",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("template_step_id", sa.UUID(), nullable=True),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("resolution", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("raised_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name="fk_task_steps_task_id_tasks", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["template_step_id"],
            ["task_type_steps.id"],
            name="fk_task_steps_template_step_id_task_type_steps",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name="fk_task_steps_run_id_runs", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_task_steps"),
    )
    op.create_index("ix_task_steps_task_seq", "task_steps", ["task_id", "seq"])

    # ---------- L3 归属层 ----------
    op.create_table(
        "task_type_capabilities",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("task_type_id", sa.UUID(), nullable=False),
        sa.Column("capability_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["task_type_id"],
            ["task_types.id"],
            name="fk_task_type_capabilities_task_type_id_task_types",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["capability_id"],
            ["capabilities.id"],
            name="fk_task_type_capabilities_capability_id_capabilities",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_task_type_capabilities"),
        sa.UniqueConstraint(
            "task_type_id",
            "capability_id",
            name="uq_task_type_capabilities_task_type_id",
        ),
    )
    op.create_index(
        "ix_task_type_capabilities_capability_id", "task_type_capabilities", ["capability_id"]
    )
    op.create_index(
        "ix_task_type_capabilities_task_type_id", "task_type_capabilities", ["task_type_id"]
    )

    # ---------- 回填 1：内建通用任务集 ----------
    op.execute(
        sa.text(
            """
            INSERT INTO task_types
                (id, name, description, kind, sort_order, enabled, created_at, updated_at)
            SELECT gen_random_uuid(),
                   :name,
                   '通用任务集：不属于任何特定主任务的能力与历史会话都归于此，'
                   '作为所有主任务的能力兜底。',
                   'common',
                   0,
                   true,
                   now(),
                   now()
            WHERE NOT EXISTS (SELECT 1 FROM task_types WHERE name = :name)
            """
        ).bindparams(name=COMMON_TASK_TYPE_NAME)
    )

    # ---------- 回填 2：既有能力全部归属「通用任务」 ----------
    op.execute(
        sa.text(
            """
            INSERT INTO task_type_capabilities (id, task_type_id, capability_id, created_at)
            SELECT gen_random_uuid(), ct.id, c.id, now()
            FROM capabilities c
            CROSS JOIN (SELECT id FROM task_types WHERE name = :name) ct
            WHERE NOT EXISTS (
                SELECT 1 FROM task_type_capabilities ttc
                WHERE ttc.capability_id = c.id AND ttc.task_type_id = ct.id
            )
            """
        ).bindparams(name=COMMON_TASK_TYPE_NAME)
    )

    # ---------- 回填 3：既有会话各建一条主任务实例 ----------
    # 步骤为空（progress_total=0）；closed/archived 视为已完成，active 保持进行中。
    op.execute(
        sa.text(
            """
            INSERT INTO tasks
                (id, task_type_id, agent_id, conversation_id, title, status,
                 progress_done, progress_total, out_of_scope,
                 started_at, finished_at, created_at, updated_at)
            SELECT gen_random_uuid(),
                   ct.id,
                   c.agent_id,
                   c.id,
                   c.title,
                   CASE WHEN c.status = 'active' THEN 'active' ELSE 'done' END,
                   0,
                   0,
                   '[]'::jsonb,
                   c.created_at,
                   CASE WHEN c.status = 'active' THEN NULL ELSE c.updated_at END,
                   now(),
                   now()
            FROM conversations c
            CROSS JOIN (SELECT id FROM task_types WHERE name = :name) ct
            WHERE NOT EXISTS (SELECT 1 FROM tasks t WHERE t.conversation_id = c.id)
            """
        ).bindparams(name=COMMON_TASK_TYPE_NAME)
    )


def downgrade() -> None:
    op.drop_index("ix_task_type_capabilities_task_type_id", table_name="task_type_capabilities")
    op.drop_index("ix_task_type_capabilities_capability_id", table_name="task_type_capabilities")
    op.drop_table("task_type_capabilities")
    op.drop_index("ix_task_steps_task_seq", table_name="task_steps")
    op.drop_table("task_steps")
    op.drop_index("ix_tasks_type_status", table_name="tasks")
    op.drop_index("ix_tasks_status", table_name="tasks")
    op.drop_table("tasks")
    op.drop_index("ix_task_type_steps_type_seq", table_name="task_type_steps")
    op.drop_table("task_type_steps")
    op.drop_table("task_types")
