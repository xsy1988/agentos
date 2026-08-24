"""memory 路由：记忆浏览 / 手改 / 手动触发整理 Job（个人平台红利：记忆可直接手改）。"""

import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.modules.auth.deps import get_current_user
from app.modules.memory import service
from app.modules.memory.models import MemoryFile

router = APIRouter(
    prefix="/memory",
    tags=["memory"],
    dependencies=[Depends(get_current_user)],
)


class MemoryOut(BaseModel):
    id: str
    kind: str
    date: str | None
    title: str
    content: str
    token_count: int
    source_run_ids: list[str] = []


class MemoryUpdateIn(BaseModel):
    title: str | None = None
    content: str | None = Field(default=None, min_length=1)


class ConsolidateIn(BaseModel):
    target: date | None = None  # 缺省整理昨天


def _to_out(m: MemoryFile) -> MemoryOut:
    return MemoryOut(
        id=str(m.id),
        kind=m.kind,
        date=str(m.date) if m.date else None,
        title=m.title,
        content=m.content,
        token_count=m.token_count,
        source_run_ids=[str(r) for r in (m.source_run_ids or [])],
    )


@router.get("", response_model=list[MemoryOut])
async def list_memories(
    kind: str | None = None, db: AsyncSession = Depends(get_db)
) -> list[MemoryOut]:
    q = select(MemoryFile).order_by(MemoryFile.kind, MemoryFile.date.desc())
    if kind:
        q = q.where(MemoryFile.kind == kind)
    rows = (await db.execute(q)).scalars().all()
    return [_to_out(m) for m in rows]


@router.patch("/{memory_id}", response_model=MemoryOut)
async def update_memory(
    memory_id: uuid.UUID, body: MemoryUpdateIn, db: AsyncSession = Depends(get_db)
) -> MemoryOut:
    """手改记忆：内容/标题，token_count 重算（向量不更新，注入用全文）。"""
    q = select(MemoryFile).where(MemoryFile.id == memory_id)
    m = (await db.execute(q)).scalar_one_or_none()
    if m is None:
        raise HTTPException(status_code=404, detail="记忆不存在")
    if body.title is not None:
        m.title = body.title
    if body.content is not None:
        m.content = body.content
        m.token_count = service._est_tokens(m.content)
    await db.commit()
    await db.refresh(m)
    return _to_out(m)


@router.delete("/{memory_id}", status_code=204)
async def delete_memory(memory_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> None:
    q = select(MemoryFile).where(MemoryFile.id == memory_id)
    m = (await db.execute(q)).scalar_one_or_none()
    if m is None:
        raise HTTPException(status_code=404, detail="记忆不存在")
    await db.delete(m)
    await db.commit()


@router.post("/consolidate")
async def consolidate(body: ConsolidateIn) -> dict:
    """手动触发整理 Job（正常由 scheduler 每日 03:00 调度）。"""
    return await service.consolidate_daily(body.target)
