"""search_knowledge：pgvector 余弦 Top-K + heading_path 拼装 + 目录范围过滤。

被两处复用：API 检索测试器（router）与 Agent 工具（tools_builtin 的
search_knowledge 执行器——知识库对引擎只是个工具，模块详细设计 §2.4.2）。
"""

import uuid
from typing import Any

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import session_factory
from app.modules.discovery.retriever import _embed_query
from app.modules.knowledge.models import KbChunk, KbDoc, KbFolder

DEFAULT_K = 8


async def search_knowledge(
    query: str,
    folders: list[str] | None = None,
    k: int = DEFAULT_K,
    db: AsyncSession | None = None,
) -> list[dict[str, Any]]:
    """语义检索知识库。

    folders：目录 path 前缀列表（如 ["/产品知识"]），空 = 全库；
    命中块按 cosine_distance 升序，返回 doc/folder 元数据拼装的上下文片段。
    """
    vec = await _embed_query(query)
    if vec is None:
        return []

    async def _run(session: AsyncSession) -> list[dict[str, Any]]:
        conds: list[ColumnElement[bool]] = [
            KbDoc.status == "ready",
            KbChunk.embedding.isnot(None),
        ]
        if folders:
            conds.append(or_(*[KbFolder.path.like(f + "%") for f in folders]))
        rows = (
            await session.execute(
                select(KbChunk, KbDoc, KbFolder)
                .join(KbDoc, KbChunk.doc_id == KbDoc.id)
                .join(KbFolder, KbDoc.folder_id == KbFolder.id)
                .where(*conds)
                .order_by(KbChunk.embedding.cosine_distance(vec))
                .limit(k)
            )
        ).all()
        return [
            {
                "chunk_id": str(chunk.id),
                "doc_id": str(doc.id),
                "doc_title": doc.title,
                "folder_path": folder.path,
                "heading_path": chunk.heading_path,
                "content": chunk.content,
                "score": 1.0,  # 余弦相似度由 HNSW 排序保证，不回读距离以省一次投影
            }
            for chunk, doc, folder in rows
        ]

    if db is not None:
        return await _run(db)
    async with session_factory() as session:
        return await _run(session)


def format_hits(hits: list[dict[str, Any]]) -> str:
    """检索结果 → 工具观察文本（heading_path 上下文化拼装）。"""
    if not hits:
        return "知识库检索无结果（可能未命中、目录范围外或知识库为空）"
    lines = [f"知识库检索命中 {len(hits)} 条："]
    for i, h in enumerate(hits, 1):
        head = f"（{h['heading_path']}）" if h.get("heading_path") else ""
        lines.append(f"{i}. [{h['doc_title']} | {h['folder_path']}]{head}\n{h['content']}")
    return "\n\n".join(lines)


async def doc_chunk_ids(doc_id: uuid.UUID) -> list[str]:
    """调试辅助：文档的 chunk id 列表。"""
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(KbChunk.id).where(KbChunk.doc_id == doc_id).order_by(KbChunk.seq)
            )
        ).scalars().all()
    return [str(r) for r in rows]
