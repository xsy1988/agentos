"""users 表（单用户）。"""

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPkMixin


class User(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    api_token: Mapped[str | None] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(64), default="")
    avatar: Mapped[str | None] = mapped_column(String(255))
