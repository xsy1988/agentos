from fastapi import FastAPI

from app.core.config import settings

app = FastAPI(title=settings.app_name, version="0.1.0")


@app.get("/health")
async def health() -> dict[str, str]:
    """存活探针：阶段 0 DoD 验收点。"""
    return {"status": "ok"}
