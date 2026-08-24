"""knowledge 路由：目录树 + 文档管道 + 检索测试器 + reindex。"""

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.modules.auth.deps import get_current_user
from app.modules.knowledge import pipeline, service
from app.modules.knowledge.models import KbDoc
from app.modules.knowledge.parser import parser_health
from app.modules.knowledge.schemas import (
    DocCreateIn,
    DocOut,
    FolderCreateIn,
    FolderOut,
    FolderUpdateIn,
    SearchHit,
    SearchIn,
)
from app.modules.knowledge.search import search_knowledge

router = APIRouter(
    prefix="/kb",
    tags=["knowledge"],
    dependencies=[Depends(get_current_user)],
)


def _doc_out(doc: KbDoc) -> DocOut:
    return DocOut(
        id=str(doc.id),
        folder_id=str(doc.folder_id),
        title=doc.title,
        source_file_id=str(doc.source_file_id) if doc.source_file_id else None,
        source_type=doc.source_type,
        status=doc.status,
        error=doc.error,
        chunk_count=doc.chunk_count,
        embedding_model=doc.embedding_model,
    )


# ---------- 目录树 ----------


@router.get("/folders", response_model=list[FolderOut])
async def list_folders(db: AsyncSession = Depends(get_db)) -> list[dict]:
    return await service.list_folders(db)


@router.post("/folders", response_model=FolderOut, status_code=201)
async def create_folder(body: FolderCreateIn, db: AsyncSession = Depends(get_db)) -> FolderOut:
    folder = await service.create_folder(
        db,
        body.name,
        UUID(body.parent_id) if body.parent_id else None,
        body.description,
        body.sort_order,
    )
    return FolderOut(
        id=str(folder.id),
        name=folder.name,
        parent_id=str(folder.parent_id) if folder.parent_id else None,
        path=folder.path,
        description=folder.description,
        sort_order=folder.sort_order,
    )


@router.patch("/folders/{folder_id}", response_model=FolderOut)
async def update_folder(
    folder_id: UUID, body: FolderUpdateIn, db: AsyncSession = Depends(get_db)
) -> FolderOut:
    folder = await service.update_folder(
        db, folder_id, body.name, body.parent_id, body.description
    )
    return FolderOut(
        id=str(folder.id),
        name=folder.name,
        parent_id=str(folder.parent_id) if folder.parent_id else None,
        path=folder.path,
        description=folder.description,
        sort_order=folder.sort_order,
    )


@router.delete("/folders/{folder_id}", status_code=204)
async def delete_folder(folder_id: UUID, db: AsyncSession = Depends(get_db)) -> None:
    await service.delete_folder(db, folder_id)


# ---------- 文档管道 ----------


@router.get("/docs", response_model=list[DocOut])
async def list_docs(
    folder_id: UUID | None = None, db: AsyncSession = Depends(get_db)
) -> list[DocOut]:
    q = select(KbDoc).order_by(KbDoc.created_at.desc())
    if folder_id is not None:
        q = q.where(KbDoc.folder_id == folder_id)
    docs = (await db.execute(q)).scalars().all()
    return [_doc_out(d) for d in docs]


@router.post("/docs", response_model=DocOut, status_code=201)
async def create_doc(body: DocCreateIn, db: AsyncSession = Depends(get_db)) -> DocOut:
    """从已上传文件创建知识文档，管道后台启动（uploaded → … → ready）。"""
    doc = await pipeline.create_doc(
        db, UUID(body.file_id), UUID(body.folder_id), body.title
    )
    return _doc_out(doc)


@router.post("/docs/{doc_id}/retry", response_model=DocOut)
async def retry_doc(doc_id: UUID, db: AsyncSession = Depends(get_db)) -> DocOut:
    """断点重试：按存量推断从 parsing / chunking / embedding 继续。"""
    doc = await pipeline.retry_doc(db, doc_id)
    return _doc_out(doc)


@router.delete("/docs/{doc_id}", status_code=204)
async def delete_doc(doc_id: UUID, db: AsyncSession = Depends(get_db)) -> None:
    doc = (await db.execute(select(KbDoc).where(KbDoc.id == doc_id))).scalar_one_or_none()
    if doc is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="文档不存在")
    from sqlalchemy import delete as sa_delete

    from app.modules.knowledge.models import KbChunk

    await db.execute(sa_delete(KbChunk).where(KbChunk.doc_id == doc_id))
    await db.delete(doc)
    await db.commit()


# ---------- 检索测试器 / reindex ----------


@router.post("/search", response_model=list[SearchHit])
async def search(body: SearchIn, db: AsyncSession = Depends(get_db)) -> list[SearchHit]:
    """检索测试器（与 Agent 的 search_knowledge 工具同构）。"""
    hits = await search_knowledge(body.query, body.folders or None, body.k, db=db)
    return [SearchHit(**h) for h in hits]


@router.post("/reindex")
async def reindex() -> dict[str, int]:
    """全量重建向量（换 embedding 模型后触发）：ready 文档重跑 embedding。"""
    count = await pipeline.reindex_all()
    return {"scheduled": count}


@router.get("/parser-health")
async def parser_health_ep() -> dict[str, bool]:
    """解析容器（docling-serve）健康状态。"""
    return {"healthy": await parser_health()}
