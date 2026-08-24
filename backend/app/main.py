"""FastAPI 应用入口。"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import settings
from app.db import models  # noqa: F401 注册全部模型，保证 FK 可解析
from app.modules.agents.router import router as agents_router
from app.modules.agents.service import seed_default_agent
from app.modules.auth.router import router as auth_router
from app.modules.capabilities.mcp_client import mcp_pool
from app.modules.capabilities.router import (
    bindings_router as capability_bindings_router,
)
from app.modules.capabilities.router import router as capabilities_router
from app.modules.capabilities.service import seed_builtin_capabilities
from app.modules.conversations.router import router as conversations_router
from app.modules.engine.runtime import engine_runtime
from app.modules.models_module.router import router as models_router
from app.modules.runs.router import router as runs_router

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # 幂等 seed：首次启动创建默认 Agent，保证零配置可用
    await seed_default_agent()
    # 幂等 seed：builtin 占位工具（2c DoD 工具链路验证用）
    await seed_builtin_capabilities()
    # 引擎运行时：checkpointer + 图 + inbox worker（单进程纪律：只有这一份）
    await engine_runtime.start()
    # MCP 连接池：按注册表拉起 + 60s 健康检查 + capability_changed 热注册
    await mcp_pool.start()
    yield
    await mcp_pool.stop()
    await engine_runtime.stop()


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

API_PREFIX = "/api/v1"
app.include_router(auth_router, prefix=API_PREFIX)
app.include_router(agents_router, prefix=API_PREFIX)
app.include_router(models_router, prefix=API_PREFIX)
app.include_router(conversations_router, prefix=API_PREFIX)
app.include_router(runs_router, prefix=API_PREFIX)
app.include_router(capabilities_router, prefix=API_PREFIX)
app.include_router(capability_bindings_router, prefix=API_PREFIX)


@app.get("/health")
async def health() -> dict[str, str]:
    """存活探针：不依赖数据库，纯进程存活。"""
    return {"status": "ok"}
