"""files 路由：上传（multipart）。"""

from fastapi import APIRouter, Depends, UploadFile
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.modules.auth.deps import get_current_user
from app.modules.files import service

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
