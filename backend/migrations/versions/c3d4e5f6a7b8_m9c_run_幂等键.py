"""m9c：run 级幂等键（方案 §5 P1-9）。

问题：`POST /conversations/{id}/messages` 无幂等键——前端连点两次提交就是两个
run（双倍 token、双份副作用），网络超时后的重试同理。客户端 `client_message_id`
此前无处可落。

本迁移只加一个**部分唯一表达式索引**（不改列、不动数据）：

- 键落在 `runs.input` 内（`input ->> 'client_message_id'`），run 快照自解释，
  与 `task_id` / `worker_name` 等既有 input 字段一致；
- 唯一性范围是**同会话内**：不同会话各自发生同一键不该互相顶掉（键由前端
  UUID 生成，跨会话重复本身即异常，不做全局唯一以免误伤）；
- `WHERE input ? 'client_message_id'` 让定时器/回调续跑等不带该键的 run 完全
  不受约束，索引本身也只覆盖带键行。

存量数据：无需回填（历史 run 无该键，索引自动跳过）。
"""

from alembic import op

revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE UNIQUE INDEX uq_runs_client_message_id
            ON runs (conversation_id, (input ->> 'client_message_id'))
            WHERE input ? 'client_message_id'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_runs_client_message_id")
