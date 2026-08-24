"""notifications 路由：通知中心（列表 / 已读 / 全部已读）。"""

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.modules.auth.deps import get_current_user
from app.modules.notifications.models import Notification

router = APIRouter(
    prefix="/notifications",
    tags=["notifications"],
    dependencies=[Depends(get_current_user)],
)


class NotificationOut(BaseModel):
    id: str
    run_id: str | None
    kind: str
    title: str
    content: str
    url: str | None
    read: bool
    created_at: str


@router.get("", response_model=list[NotificationOut])
async def list_notifications(
    unread_only: bool = False, limit: int = 50, db: AsyncSession = Depends(get_db)
) -> list[NotificationOut]:
    q = select(Notification).order_by(Notification.created_at.desc()).limit(min(limit, 200))
    if unread_only:
        q = q.where(Notification.read.is_(False))
    rows = (await db.execute(q)).scalars().all()
    return [
        NotificationOut(
            id=str(n.id),
            run_id=str(n.run_id) if n.run_id else None,
            kind=n.kind,
            title=n.title,
            content=n.content,
            url=n.url,
            read=n.read,
            created_at=n.created_at.isoformat(),
        )
        for n in rows
    ]


@router.post("/{notification_id}/read", status_code=204)
async def mark_read(notification_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> None:
    await db.execute(
        update(Notification).where(Notification.id == notification_id).values(read=True)
    )
    await db.commit()


@router.post("/read-all", status_code=204)
async def mark_all_read(db: AsyncSession = Depends(get_db)) -> None:
    await db.execute(update(Notification).values(read=True))
    await db.commit()
