"""m3a：embedding 维度 1536→1024（Kimi bge_m3_embed 实测维度）

现有 embedding 值全为 NULL，直接 cast 安全；换 embedding 模型的重建纪律
见设计方案 §4（数据库设计：三处向量列统一维度）。
"""

from alembic import op
from pgvector.sqlalchemy import Vector

revision = "c4e1f0a3b7d2"
down_revision = "b3f7a91c2d40"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "capabilities",
        "embedding",
        type_=Vector(1024),
        existing_type=Vector(1536),
        postgresql_using="embedding::vector(1024)",
    )


def downgrade() -> None:
    op.alter_column(
        "capabilities",
        "embedding",
        type_=Vector(1536),
        existing_type=Vector(1024),
        postgresql_using="embedding::vector(1536)",
    )
