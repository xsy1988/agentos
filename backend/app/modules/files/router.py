"""files 路由：上传（multipart）+ 内容下载（消息附件展示用）。"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.modules.auth.deps import get_current_user
from app.modules.files import service
from app.modules.files.models import File

router = APIRouter(
    prefix="/files",
    tags=["files"],
    dependencies=[Depends(get_current_user)],
)


class FileOut(BaseModel):
    id: str
    path: str
    filename: str
    mime: str
    size: int
    sha256: str
    deduplicated: bool = False


@router.post("/upload", response_model=FileOut)
async def upload_file(
    file: UploadFile,
    db: AsyncSession = Depends(get_db),
) -> FileOut:
    """上传统一入口：sha256 去重，knowledge / 消息附件复用。"""
    content = await file.read()
    saved, dedup = await service.save_upload(
        db, file.filename or "unnamed", content
    )
    return FileOut(
        id=str(saved.id),
        path=saved.path,
        filename=saved.filename,
        mime=saved.mime,
        size=saved.size,
        sha256=saved.sha256,
        deduplicated=dedup,
    )


@router.get("/{file_id}/content")
async def get_file_content(
    file_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    """附件内容下载（消息区图片/文档展示）。路径由 DB 查出，不接收路径参数。"""
    file = await db.get(File, file_id)
    if file is None:
        raise HTTPException(status_code=404, detail="文件不存在")
    abs_path = settings.data_dir / file.path
    if not abs_path.is_file():
        raise HTTPException(status_code=404, detail="文件内容缺失（可能被清理）")
    return FileResponse(
        path=abs_path,
        media_type=file.mime or "application/octet-stream",
        filename=file.filename,
    )
