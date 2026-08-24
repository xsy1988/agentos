"""knowledge 管道：解析→切分→embedding→ready，状态机 + 断点重试。

- API 服务内 asyncio 后台任务，纯 I/O（模块详细设计 §2.4.1）
- 每步失败落库 status=failed + error；retry 按存量推断断点继续：
  解析中间产物存 data/parse/{doc_id}.md，chunks 先入库（无向量）再补向量
- 进程重启时中间态文档标记 failed（可 retry），不自动续跑（幂等由 retry 保证）
"""

import asyncio
import logging
import uuid
from pathlib import Path

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import session_factory
from app.modules.files import service as files_service
from app.modules.files.models import File
from app.modules.knowledge import parser
from app.modules.knowledge.chunker import chunk_markdown
from app.modules.knowledge.models import KbChunk, KbDoc

logger = logging.getLogger(__name__)

# 运行中的管道任务（防同文档并发处理）
_running: dict[str, asyncio.Task[None]] = {}

EMBED_BATCH = 8  # embedding 批大小（设计：8 并发）


def _parse_path(doc_id: str) -> Path:
    return settings.data_dir / "parse" / f"{doc_id}.md"


async def create_doc(
    db: AsyncSession, file_id: uuid.UUID, folder_id: uuid.UUID, title: str | None
) -> KbDoc:
    """从已上传文件创建知识文档（status=uploaded），登记引用并启动管道。"""
    file = (
        await db.execute(select(File).where(File.id == file_id))
    ).scalar_one_or_none()
    if file is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="文件不存在")
    source_type = Path(file.filename).suffix.lower().lstrip(".") or "txt"
    doc = KbDoc(
        folder_id=folder_id,
        title=title or Path(file.filename).stem,
        source_file_id=file.id,
        source_type=source_type,
        status="uploaded",
        meta={"filename": file.filename, "mime": file.mime},
    )
    db.add(doc)
    await db.flush()
    await files_service.add_ref(db, file.id, "doc", doc.id)
    await db.commit()
    await db.refresh(doc)
    start_pipeline(str(doc.id))
    return doc


def start_pipeline(doc_id: str) -> None:
    """启动（或复用）后台管道任务。"""
    task = _running.get(doc_id)
    if task is not None and not task.done():
        return
    _running[doc_id] = asyncio.create_task(_process(doc_id))


async def _process(doc_id: str) -> None:
    """管道主体：parsing → chunking → embedding → ready。"""
    try:
        await _step_parsing(doc_id)
        await _step_chunking(doc_id)
        await _step_embedding(doc_id)
    except Exception as exc:  # noqa: BLE001 管道任何一步失败都要落库可读原因
        logger.exception("kb 管道失败 doc=%s", doc_id)
        async with session_factory() as db:
            await db.execute(
                update(KbDoc).where(KbDoc.id == uuid.UUID(doc_id)).values(
                    status="failed", error=str(exc)[:2000]
                )
            )
            await db.commit()
    finally:
        _running.pop(doc_id, None)


async def _step_parsing(doc_id: str) -> None:
    """解析：读源文件 → docling 容器 → markdown 中间产物落盘。

    已有中间产物则跳过（断点重试；reindex/强制重解析先删产物）。
    """
    did = uuid.UUID(doc_id)
    p = _parse_path(doc_id)
    async with session_factory() as db:
        doc = (await db.execute(select(KbDoc).where(KbDoc.id == did))).scalar_one()
        if p.exists():
            return  # 断点：解析已完成
        await db.execute(update(KbDoc).where(KbDoc.id == did).values(status="parsing", error=None))
        await db.commit()

        file = (
            await db.execute(select(File).where(File.id == doc.source_file_id))
        ).scalar_one_or_none()
        if file is None:
            raise RuntimeError("源文件记录不存在（可能被清理）")
        content = (settings.data_dir / file.path).read_bytes()

    md = await parser.parse_document(file.filename, content)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(md, encoding="utf-8")


async def _step_chunking(doc_id: str) -> None:
    """切分：markdown → 512±128 token 块（heading_path 元数据）→ kb_chunks（无向量）。

    幂等：先删旧 chunks 再写（重试/reindex 安全）。
    """
    did = uuid.UUID(doc_id)
    p = _parse_path(doc_id)
    if not p.exists():
        raise RuntimeError("解析中间产物缺失，应先完成 parsing")
    md = p.read_text(encoding="utf-8")
    pieces = chunk_markdown(md)
    if not pieces:
        raise RuntimeError("切分结果为空：文档无有效文本内容")

    async with session_factory() as db:
        await db.execute(
            update(KbDoc).where(KbDoc.id == did).values(status="chunking", error=None)
        )
        await db.execute(delete(KbChunk).where(KbChunk.doc_id == did))
        for seq, piece in enumerate(pieces):
            db.add(
                KbChunk(
                    doc_id=did,
                    seq=seq,
                    content=piece["content"],
                    heading_path=piece["heading_path"],
                    token_count=piece["token_count"],
                )
            )
        await db.commit()


async def _step_embedding(doc_id: str) -> None:
    """embedding：chunks 批量向量化补齐 → ready。

    幂等：重算全部向量（reindex 换模型 = 对全部 ready 文档重跑本步骤）。
    """
    from app.modules.models_module.models import ModelProvider
    from app.modules.models_module.provider import decrypt_secret, get_embeddings

    did = uuid.UUID(doc_id)
    async with session_factory() as db:
        await db.execute(
            update(KbDoc).where(KbDoc.id == did).values(status="embedding", error=None)
        )
        await db.commit()

        prov = await db.scalar(
            select(ModelProvider).where(
                ModelProvider.kind == "embedding",
                ModelProvider.status == "enabled",
            )
        )
        if prov is None:
            raise RuntimeError("无可用 embedding provider（kind=embedding 且 enabled）")
        api_key = decrypt_secret(prov.api_key_encrypted) if prov.api_key_encrypted else None
        embeddings = get_embeddings(prov, api_key)

        chunks = (
            (await db.execute(select(KbChunk).where(KbChunk.doc_id == did).order_by(KbChunk.seq)))
            .scalars().all()
        )
        if not chunks:
            raise RuntimeError("chunks 缺失，应先完成 chunking")

        for i in range(0, len(chunks), EMBED_BATCH):
            batch = chunks[i : i + EMBED_BATCH]
            vectors = await embeddings.aembed_documents([c.content for c in batch])
            for chunk, vec in zip(batch, vectors, strict=True):
                chunk.embedding = vec
                chunk.embedding_model = prov.model_name
                chunk.dim = len(vec)
        await db.execute(
            update(KbDoc)
            .where(KbDoc.id == did)
            .values(
                status="ready",
                error=None,
                chunk_count=len(chunks),
                embedding_model=prov.model_name,
            )
        )
        await db.commit()


async def retry_doc(db: AsyncSession, doc_id: uuid.UUID) -> KbDoc:
    """断点重试：按存量推断从 parsing / chunking / embedding 继续。"""
    doc = (await db.execute(select(KbDoc).where(KbDoc.id == doc_id))).scalar_one_or_none()
    if doc is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="文档不存在")
    if doc.status != "failed":
        from fastapi import HTTPException

        raise HTTPException(status_code=409, detail=f"仅 failed 状态可重试（当前 {doc.status}）")

    # 断点推断（读 chunks 存量在管道步骤内自行幂等处理）：
    # 无解析产物 → parsing；有产物无 chunks → chunking；有 chunks → embedding
    async with session_factory() as db2:
        has_chunks = await db2.scalar(
            select(KbChunk.id).where(KbChunk.doc_id == doc_id).limit(1)
        )
    p = _parse_path(str(doc_id))
    if not p.exists():
        resume = "parsing"
    elif has_chunks is None:
        resume = "chunking"
    else:
        resume = "embedding"

    # resume=chunking 时删旧 chunks 由步骤自身幂等；resume=embedding 直接补向量
    doc.status = resume
    doc.error = None
    await db.commit()
    await db.refresh(doc)
    start_pipeline(str(doc_id))
    return doc


async def reindex_all() -> int:
    """全量重建向量（换 embedding 模型后触发）：所有 ready 文档重跑 embedding 步骤。

    chunks 文本不变、只重算向量；旧向量直接覆盖（共存回退不做版本化，个人平台
    从简——回退 = 换回旧模型再 reindex）。
    """
    async with session_factory() as db:
        doc_ids = (
            (await db.execute(select(KbDoc.id).where(KbDoc.status == "ready"))).scalars().all()
        )
    for did in doc_ids:
        async with session_factory() as db:
            await db.execute(
                update(KbDoc).where(KbDoc.id == did).values(status="embedding", error=None)
            )
            await db.commit()
        start_pipeline(str(did))
    return len(doc_ids)


async def reconcile_interrupted() -> None:
    """进程启动时：中间态（parsing/chunking/embedding）文档 → failed（可 retry）。"""
    async with session_factory() as db:
        result = await db.execute(
            select(KbDoc.id).where(KbDoc.status.in_(("parsing", "chunking", "embedding")))
        )
        stuck = result.scalars().all()
        for did in stuck:
            await db.execute(
                update(KbDoc)
                .where(KbDoc.id == did)
                .values(status="failed", error="进程重启，管道中断（可重试）")
            )
        if stuck:
            await db.commit()
