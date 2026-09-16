"""m8：Worker 文件化——task_types 三表退役，tasks/task_steps 直指文件包。

Worker 定义已完全文件化（data/workers/<name>/<version>/WORKER.md 文件包，
app/modules/workers/registry.py 为唯一读取入口）：
- tasks：task_type_id FK → worker_name + worker_version（任务创建时锁定生效版本）；
- task_steps：template_step_id FK → worker_step_ref（sub_workers/<ref> 文件夹名）。

迁移前置：先跑 scripts/export_workers_to_files.py 把 DB 模板导出为文件包
（任务实例按 name 回填 worker_name、版本统一为 v1；通用任务集不导出，
由内建常量 COMMON_WORKER 兜底）。
"""

import sqlalchemy as sa
from alembic import op

revision = "g8d9e0f1a2b3"
down_revision = "f2b3c4d5e6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---------- tasks：绑定 Worker（name + version） ----------
    op.add_column(
        "tasks",
        sa.Column(
            "worker_name", sa.String(length=128), nullable=False, server_default="__common__"
        ),
    )
    op.add_column(
        "tasks",
        sa.Column("worker_version", sa.String(length=16), nullable=False, server_default=""),
    )
    op.execute(
        sa.text(
            """
            UPDATE tasks t
            SET worker_name = tt.name,
                worker_version = 'v1'
            FROM task_types tt
            WHERE t.task_type_id = tt.id
            """
        )
    )
    # 通用任务集 → 内建 COMMON_WORKER（不再落库）
    op.execute(
        sa.text(
            "UPDATE tasks SET worker_name = '__common__', worker_version = '' "
            "WHERE worker_name = '通用任务'"
        )
    )
    # 回填完成后撤掉 server_default（默认值只保留在 ORM 侧，避免双源）
    op.alter_column("tasks", "worker_name", server_default=None)
    op.alter_column("tasks", "worker_version", server_default=None)

    # ---------- task_steps：回指 sub_workers 文件夹名 ----------
    op.add_column("task_steps", sa.Column("worker_step_ref", sa.String(length=255), nullable=True))
    op.execute(
        sa.text(
            """
            UPDATE task_steps ts
            SET worker_step_ref = tts.name
            FROM task_type_steps tts
            WHERE ts.template_step_id = tts.id
            """
        )
    )

    # ---------- 退役定义层三表 ----------
    op.drop_constraint("fk_tasks_task_type_id_task_types", "tasks", type_="foreignkey")
    op.drop_index("ix_tasks_type_status", table_name="tasks")
    op.drop_column("tasks", "task_type_id")
    op.drop_constraint(
        "fk_task_steps_template_step_id_task_type_steps", "task_steps", type_="foreignkey"
    )
    op.drop_column("task_steps", "template_step_id")
    op.create_index("ix_tasks_worker_status", "tasks", ["worker_name", "status"])

    op.drop_table("task_type_capabilities")
    op.drop_index("ix_task_type_steps_type_seq", table_name="task_type_steps")
    op.drop_table("task_type_steps")
    op.drop_table("task_types")


def downgrade() -> None:
    """尽力回滚：重建定义层三表（数据无法从文件包恢复，仅恢复结构）。"""
    op.create_table(
        "task_types",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("playbook", sa.Text(), nullable=False),
        sa.Column("references", sa.JSON(), nullable=True),
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
        sa.Column("playbook", sa.Text(), nullable=False),
        sa.Column("references", sa.JSON(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("optional", sa.Boolean(), nullable=False),
        sa.Column("capability_hint", sa.JSON(), nullable=True),
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
            "task_type_id", "capability_id", name="uq_task_type_capabilities_task_type_id"
        ),
    )

    op.drop_index("ix_tasks_worker_status", table_name="tasks")
    op.add_column(
        "tasks",
        sa.Column("task_type_id", sa.UUID(), nullable=True),
    )
    op.create_index("ix_tasks_type_status", "tasks", ["task_type_id", "status"])
    op.add_column(
        "task_steps",
        sa.Column("template_step_id", sa.UUID(), nullable=True),
    )
    op.drop_column("task_steps", "worker_step_ref")
    op.drop_column("tasks", "worker_version")
    op.drop_column("tasks", "worker_name")
