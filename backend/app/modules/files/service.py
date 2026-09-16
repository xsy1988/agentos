"""files 服务：上传落盘 + sha256 去重 + 引用登记。"""

import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.modules.files.models import File, FileRef

# 允许上传的扩展名（知识库管道 / 对话附件共用上传入口）：
# 文档 → source_type（知识库管道只处理这些）；图片仅供对话附件（多模态投喂）
DOC_EXTS = {".pdf": "pdf", ".docx": "word", ".md": "md", ".txt": "txt", ".xlsx": "excel"}
IMAGE_EXTS = {".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image", ".gif": "image"}
ALLOWED_EXTS = DOC_EXTS | IMAGE_EXTS

# 对话附件限制（方案拍板：图片 5MB / 文档 10MB；单条消息 ≤5 个由 MessageIn 校验）
MAX_IMAGE_SIZE = 5 * 1024 * 1024
MAX_DOC_SIZE = 10 * 1024 * 1024


def _storage_path(ext: str) -> Path:
    """data/files/{YYYY-MM}/{uuid}{ext}。"""
    month = datetime.now(UTC).strftime("%Y-%m")
    rel = Path("data/files") / month / f"{uuid.uuid4()}{ext}"
    return rel


async def save_upload(db: AsyncSession, filename: str, content: bytes) -> tuple[File, bool]:
    """登记上传文件：sha256 去重（重复上传直接复用已有记录）。返回 (file, deduplicated)。"""
    sha = hashlib.sha256(content).hexdigest()
    existing = (await db.execute(select(File).where(File.sha256 == sha))).scalar_one_or_none()
    if existing is not None:
        return existing, True

    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(
            status_code=415,
            detail=f"不支持的文件类型 {ext}，允许：{', '.join(sorted(ALLOWED_EXTS))}",
        )
    limit = MAX_IMAGE_SIZE if ext in IMAGE_EXTS else MAX_DOC_SIZE
    if len(content) > limit:
        raise HTTPException(
            status_code=413,
            detail=f"文件超出大小限制（{limit // 1024 // 1024}MB）：{filename}",
        )

    rel = _storage_path(ext)
    abs_path = settings.data_dir / rel
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(content)

    file = File(
        path=str(rel),
        filename=filename,
        mime=_guess_mime(ext),
        size=len(content),
        sha256=sha,
    )
    db.add(file)
    await db.commit()
    await db.refresh(file)
    return file, False


async def add_ref(
    db: AsyncSession, file_id: uuid.UUID, ref_type: str, ref_id: uuid.UUID
) -> FileRef:
    """登记引用关系（doc / message / proposal）；已存在则幂等返回。"""
    existing = (
        await db.execute(
            select(FileRef).where(
                FileRef.file_id == file_id,
                FileRef.ref_type == ref_type,
                FileRef.ref_id == ref_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    ref = FileRef(file_id=file_id, ref_type=ref_type, ref_id=ref_id)
    db.add(ref)
    await db.flush()
    return ref


def _guess_mime(ext: str) -> str:
    return {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".md": "text/markdown",
        ".txt": "text/plain",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(ext, "application/octet-stream")
