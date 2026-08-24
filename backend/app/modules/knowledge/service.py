"""knowledge 服务：目录树维护（path 物化路径）。"""

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.knowledge.models import KbDoc, KbFolder

# 三个默认根目录（设计 §kb_folders）：幂等 seed
DEFAULT_ROOTS = ["产品知识", "研发知识", "生活知识"]


async def seed_default_folders() -> None:
    """幂等 seed：三个默认根目录。"""
    from app.core.db import session_factory

    async with session_factory() as db:
        for i, name in enumerate(DEFAULT_ROOTS):
            existing = (
                await db.execute(
                    select(KbFolder).where(KbFolder.parent_id.is_(None), KbFolder.name == name)
                )
            ).scalar_one_or_none()
            if existing is None:
                db.add(KbFolder(name=name, path=f"/{name}", sort_order=i))
        await db.commit()


async def _get_folder(db: AsyncSession, folder_id: UUID) -> KbFolder:
    folder = (
        await db.execute(select(KbFolder).where(KbFolder.id == folder_id))
    ).scalar_one_or_none()
    if folder is None:
        raise HTTPException(status_code=404, detail="目录不存在")
    return folder


async def _check_sibling_name(
    db: AsyncSession, parent_id: UUID | None, name: str, exclude_id: UUID | None = None
) -> None:
    parent_cond = (
        KbFolder.parent_id.is_(None) if parent_id is None else (KbFolder.parent_id == parent_id)
    )
    q = select(KbFolder).where(parent_cond, KbFolder.name == name)
    dup = (await db.execute(q)).scalars().all()
    if any(f.id != exclude_id for f in dup):
        raise HTTPException(status_code=409, detail=f"同级已存在同名目录「{name}」")


async def list_folders(db: AsyncSession) -> list[dict]:
    """平铺列表（含每目录文档数），前端按 parent_id 组树。"""
    folders = (await db.execute(select(KbFolder).order_by(KbFolder.path))).scalars().all()
    rows = (
        await db.execute(select(KbDoc.folder_id, func.count()).group_by(KbDoc.folder_id))
    ).all()
    counts: dict[UUID, int] = {fid: cnt for fid, cnt in rows}
    return [
        {
            "id": str(f.id),
            "name": f.name,
            "parent_id": str(f.parent_id) if f.parent_id else None,
            "path": f.path,
            "description": f.description,
            "sort_order": f.sort_order,
            "doc_count": counts.get(f.id, 0),
        }
        for f in folders
    ]


async def create_folder(
    db: AsyncSession,
    name: str,
    parent_id: UUID | None,
    description: str | None,
    sort_order: int,
) -> KbFolder:
    await _check_sibling_name(db, parent_id, name)
    if parent_id is None:
        path = f"/{name}"
    else:
        parent = await _get_folder(db, parent_id)
        path = f"{parent.path}/{name}"
    folder = KbFolder(
        name=name, parent_id=parent_id, path=path, description=description, sort_order=sort_order
    )
    db.add(folder)
    await db.commit()
    await db.refresh(folder)
    return folder


async def update_folder(
    db: AsyncSession,
    folder_id: UUID,
    name: str | None,
    parent_id: str | None,
    description: str | None,
) -> KbFolder:
    """改名 / 移动 / 改描述；path 物化路径级联更新子孙（前缀替换）。"""
    folder = await _get_folder(db, folder_id)
    old_path = folder.path

    move_requested = parent_id is not None
    if move_requested:
        new_parent_id = UUID(parent_id) if parent_id != "" else None
        if new_parent_id == folder_id:
            raise HTTPException(status_code=400, detail="不能把自己作为父目录")
        if new_parent_id is not None:
            parent = await _get_folder(db, new_parent_id)
            if parent.path.startswith(old_path + "/"):
                raise HTTPException(status_code=400, detail="不能移动到自己的子目录下")
            folder.parent_id = new_parent_id
        else:
            folder.parent_id = None

    if name is not None and name != folder.name:
        await _check_sibling_name(db, folder.parent_id, name, exclude_id=folder_id)
        folder.name = name

    if description is not None:
        folder.description = description

    # 重算自身 path
    if folder.parent_id is None:
        folder.path = f"/{folder.name}"
    else:
        parent = await _get_folder(db, folder.parent_id)
        folder.path = f"{parent.path}/{folder.name}"
    await db.flush()

    # 子孙前缀替换：/old/... → /new/...（old 前缀整体换成 new）
    if folder.path != old_path:
        await db.execute(
            text(
                "UPDATE kb_folders SET path = :new || substring(path from char_length(:old) + 1) "
                "WHERE path LIKE :old || '/%'"
            ),
            {"new": folder.path, "old": old_path},
        )
    await db.commit()
    await db.refresh(folder)
    return folder


async def delete_folder(db: AsyncSession, folder_id: UUID) -> None:
    folder = await _get_folder(db, folder_id)
    children = (
        await db.execute(select(KbFolder).where(KbFolder.parent_id == folder_id))
    ).scalar_one_or_none()
    if children is not None:
        raise HTTPException(status_code=409, detail="目录下还有子目录，不能删除")
    docs = (
        await db.execute(select(KbDoc).where(KbDoc.folder_id == folder_id).limit(1))
    ).scalar_one_or_none()
    if docs is not None:
        raise HTTPException(status_code=409, detail="目录下还有文档，不能删除")
    await db.delete(folder)
    await db.commit()
