"""knowledge 路由：目录树 + 文档管道 + 切片管理 + 检索测试器 + reindex。"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.modules.auth.deps import get_current_user
from app.modules.knowledge import pipeline, service
from app.modules.knowledge.models import KbChunk, KbDoc
from app.modules.knowledge.parser import parser_health
from app.modules.knowledge.schemas import (
    ChunkOut,
    ChunkUpdateIn,
    DocCreateIn,
    DocDetailOut,
    DocOut,
    FolderCreateIn,
    FolderOut,
    FolderUpdateIn,
    ParsePreviewOut,
    RechunkIn,
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
    folder = await service.update_folder(db, folder_id, body.name, body.parent_id, body.description)
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


@router.get("/docs/{doc_id}", response_model=DocDetailOut)
async def get_doc(doc_id: UUID, db: AsyncSession = Depends(get_db)) -> DocDetailOut:
    """文档详情：管道状态 + 已向量化块数 + 解析产物存在性（详情 Drawer 用）。"""
    doc = (await db.execute(select(KbDoc).where(KbDoc.id == doc_id))).scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    embedded = await db.scalar(
        select(func.count())
        .select_from(KbChunk)
        .where(KbChunk.doc_id == doc_id, KbChunk.embedding.is_not(None))
    )
    out = _doc_out(doc)
    return DocDetailOut(
        **out.model_dump(),
        embedded_count=int(embedded or 0),
        parse_exists=(settings.data_dir / "parse" / f"{doc_id}.md").exists(),
    )


@router.get("/docs/{doc_id}/parse", response_model=ParsePreviewOut)
async def parse_preview(doc_id: UUID, db: AsyncSession = Depends(get_db)) -> ParsePreviewOut:
    """解析产物 markdown 预览（docling 落盘的中间产物，切分的真正输入）。"""
    doc = (await db.execute(select(KbDoc.id).where(KbDoc.id == doc_id))).scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    p = settings.data_dir / "parse" / f"{doc_id}.md"
    if not p.exists():
        raise HTTPException(status_code=404, detail="解析产物不存在（未解析或已被清理）")
    return ParsePreviewOut(markdown=p.read_text(encoding="utf-8"))


@router.post("/docs", response_model=DocOut, status_code=201)
async def create_doc(body: DocCreateIn, db: AsyncSession = Depends(get_db)) -> DocOut:
    """从已上传文件创建知识文档，管道后台启动（uploaded → … → ready）。"""
    doc = await pipeline.create_doc(db, UUID(body.file_id), UUID(body.folder_id), body.title)
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


# ---------- 切片管理 ----------


@router.get("/docs/{doc_id}/chunks", response_model=list[ChunkOut])
async def list_chunks(
    doc_id: UUID,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[ChunkOut]:
    """切片分页列表（seq 升序）；返回条数 == limit 时前端可继续翻页。"""
    doc = (await db.execute(select(KbDoc.id).where(KbDoc.id == doc_id))).scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    rows = (
        (
            await db.execute(
                select(KbChunk)
                .where(KbChunk.doc_id == doc_id)
                .order_by(KbChunk.seq)
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return [
        ChunkOut(
            id=str(c.id),
            doc_id=str(c.doc_id),
            seq=c.seq,
            content=c.content,
            heading_path=c.heading_path,
            token_count=c.token_count,
            embedded=c.embedding is not None,
        )
        for c in rows
    ]


@router.patch("/chunks/{chunk_id}", response_model=ChunkOut)
async def update_chunk(
    chunk_id: UUID, body: ChunkUpdateIn, db: AsyncSession = Depends(get_db)
) -> ChunkOut:
    """编辑切片内容：修解析错字等；保存后同步重算该块向量（保持检索一致）。"""
    chunk = (await db.execute(select(KbChunk).where(KbChunk.id == chunk_id))).scalar_one_or_none()
    if chunk is None:
        raise HTTPException(status_code=404, detail="切片不存在")
    from app.modules.knowledge.chunker import est_tokens

    chunk.content = body.content
    chunk.token_count = est_tokens(body.content)
    try:
        await pipeline.embed_single_chunk(chunk)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    await db.commit()
    await db.refresh(chunk)
    return ChunkOut(
        id=str(chunk.id),
        doc_id=str(chunk.doc_id),
        seq=chunk.seq,
        content=chunk.content,
        heading_path=chunk.heading_path,
        token_count=chunk.token_count,
        embedded=chunk.embedding is not None,
    )


@router.delete("/chunks/{chunk_id}", status_code=204)
async def delete_chunk(chunk_id: UUID, db: AsyncSession = Depends(get_db)) -> None:
    """删除坏块（乱码/无价值段落）：同步递减文档块数。"""
    chunk = (await db.execute(select(KbChunk).where(KbChunk.id == chunk_id))).scalar_one_or_none()
    if chunk is None:
        raise HTTPException(status_code=404, detail="切片不存在")
    doc = await db.get(KbDoc, chunk.doc_id)
    if doc is not None and doc.chunk_count > 0:
        doc.chunk_count -= 1
    await db.delete(chunk)
    await db.commit()


@router.post("/docs/{doc_id}/rechunk", response_model=DocOut)
async def rechunk_doc(doc_id: UUID, body: RechunkIn, db: AsyncSession = Depends(get_db)) -> DocOut:
    """按自定义参数重新切分（基于既有解析产物，不重跑 docling）。"""
    doc = await pipeline.rechunk_doc(db, doc_id, body.target_tokens, body.overlap_tokens)
    return _doc_out(doc)


@router.post("/docs/{doc_id}/reembed", response_model=DocOut)
async def reembed_doc(doc_id: UUID, db: AsyncSession = Depends(get_db)) -> DocOut:
    """单文档重嵌入（reindex 的单文档版）：切片文本不变，重算全部向量。"""
    doc = await pipeline.reembed_doc(db, doc_id)
    return _doc_out(doc)


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
