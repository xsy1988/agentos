"""skill_proposals 表：技能沉淀草稿（数据库设计 §2.10）。"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import UUID, Base, TimestampMixin, UUIDPkMixin


class SkillProposal(UUIDPkMixin, TimestampMixin, Base):
    """复盘产出的 SKILL.md 草稿：proposed → 人工审核 → approved/rejected。

    approve 后转 capabilities(type=skill) 进检索池；similar_to 非空时
    草稿语义上是已有技能的修订建议而非新技能。
    """

    __tablename__ = "skill_proposals"

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("runs.id", ondelete="CASCADE"), index=True
    )
    trigger: Mapped[str] = mapped_column(String(16))  # success / correction
    draft_md: Mapped[str] = mapped_column(Text)
    similar_to_capability_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, ForeignKey("capabilities.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(16), default="proposed", index=True)
    review_note: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
