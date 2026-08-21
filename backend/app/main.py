"""FastAPI 应用入口。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import settings
from app.db import models  # noqa: F401 注册全部模型，保证 FK 可解析
from app.modules.agents.router import router as agents_router
from app.modules.agents.service import seed_default_agent
from app.modules.auth.router import router as auth_router


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # 幂等 seed：首次启动创建默认 Agent，保证零配置可用
    await seed_default_agent()
    yield


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

API_PREFIX = "/api/v1"
app.include_router(auth_router, prefix=API_PREFIX)
app.include_router(agents_router, prefix=API_PREFIX)


@app.get("/health")
async def health() -> dict[str, str]:
    """存活探针：不依赖数据库，纯进程存活。"""
    return {"status": "ok"}
