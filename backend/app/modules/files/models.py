"""files / file_refs 表：文件存储登记与引用关系。"""

import uuid

from sqlalchemy import BigInteger, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import UUID, Base, TimestampMixin, UUIDPkMixin


class File(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "files"

    path: Mapped[str] = mapped_column(String(512))  # data/files/{YYYY-MM}/{uuid}{ext}
    filename: Mapped[str] = mapped_column(String(255))
    mime: Mapped[str] = mapped_column(String(128))
    size: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)  # 去重指纹


class FileRef(UUIDPkMixin, TimestampMixin, Base):
    """引用关系：同一文件可被 doc / message / proposal 多方引用；无引用即孤儿。"""

    __tablename__ = "file_refs"

    file_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("files.id", ondelete="CASCADE"), index=True
    )
    ref_type: Mapped[str] = mapped_column(String(16))  # doc / message / proposal
    ref_id: Mapped[uuid.UUID] = mapped_column(UUID)
    note: Mapped[str | None] = mapped_column(Text)


Index("ix_file_refs_ref", FileRef.ref_type, FileRef.ref_id)
