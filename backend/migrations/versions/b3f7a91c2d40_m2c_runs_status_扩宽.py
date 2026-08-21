"""m2c：runs.status 扩宽（paused_awaiting_confirm 超 VARCHAR(16)）"""

import sqlalchemy as sa
from alembic import op

revision = "b3f7a91c2d40"
down_revision = "0162e47a6dcf"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("runs", "status", type_=sa.String(32), existing_type=sa.String(16))


def downgrade() -> None:
    op.alter_column("runs", "status", type_=sa.String(16), existing_type=sa.String(32))
