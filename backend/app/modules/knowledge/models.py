"""kb_folders 表（知识库目录树）。"""

import uuid

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import UUID, Base, TimestampMixin, UUIDPkMixin


class KbFolder(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "kb_folders"

    name: Mapped[str] = mapped_column(String(128))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, ForeignKey("kb_folders.id", ondelete="RESTRICT")
    )
    # 物化路径，如 /产品知识/硬件规格；检索按前缀圈定目录范围
    path: Mapped[str] = mapped_column(String(512), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
