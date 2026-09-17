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
from app.modules.capabilities.websearch_seed import seed_websearch_capability
from app.modules.conversations.router import router as conversations_router
from app.modules.engine.runtime import engine_runtime
from app.modules.files.router import router as files_router
from app.modules.knowledge.pipeline import reconcile_interrupted
from app.modules.knowledge.router import router as knowledge_router
from app.modules.knowledge.service import seed_default_folders
from app.modules.memory.router import router as memory_router
from app.modules.models_module.router import router as models_router
from app.modules.notifications.router import router as notifications_router
from app.modules.open_api.router import router as open_api_router
from app.modules.runs.router import artifacts_router
from app.modules.runs.router import router as runs_router
from app.modules.scheduler.router import router as scheduler_router
from app.modules.scheduler.runtime import scheduler_runtime
from app.modules.skills_forge.router import router as skills_router
from app.modules.tasks.procurement_seed import seed_procurement_worker
from app.modules.tasks.router import router as tasks_router
from app.modules.workers.router import router as workers_router

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # 幂等 seed：首次启动创建默认 Agent，保证零配置可用
    await seed_default_agent()
    # 幂等 seed：builtin 占位工具（2c DoD 工具链路验证用）+ search_knowledge
    await seed_builtin_capabilities()
    # 幂等 seed：采购报价对比 Worker 文件包 + 决策面板 plugin（决策4）
    await seed_procurement_worker()
    # 幂等 seed：自建网页搜索服务（type=mcp）。搜索栈是 compose profile，没起时探测失败
    # → 能力置 disabled/unhealthy 但不阻断启动；起来后重启后端即自动接上
    await seed_websearch_capability()
    # 幂等 seed：知识库三个默认根目录（产品/研发/生活）
    await seed_default_folders()
    # 管道中断文档 → failed（可 retry），不自动续跑
    await reconcile_interrupted()
    # 引擎运行时：checkpointer + 图 + inbox worker（单进程纪律：只有这一份）
    await engine_runtime.start()
    # MCP 连接池：按注册表拉起 + 60s 健康检查 + capability_changed 热注册
    await mcp_pool.start()
    # 调度器：timers/alarms 表全量重载 + 每日 03:00 记忆整理
    await scheduler_runtime.start()
    yield
    await scheduler_runtime.stop()
    await mcp_pool.stop()
    await engine_runtime.stop()


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

API_PREFIX = "/api/v1"
app.include_router(auth_router, prefix=API_PREFIX)
app.include_router(agents_router, prefix=API_PREFIX)
app.include_router(models_router, prefix=API_PREFIX)
app.include_router(conversations_router, prefix=API_PREFIX)
app.include_router(runs_router, prefix=API_PREFIX)
app.include_router(artifacts_router, prefix=API_PREFIX)
app.include_router(capabilities_router, prefix=API_PREFIX)
app.include_router(capability_bindings_router, prefix=API_PREFIX)
app.include_router(files_router, prefix=API_PREFIX)
app.include_router(knowledge_router, prefix=API_PREFIX)
app.include_router(memory_router, prefix=API_PREFIX)
app.include_router(scheduler_router, prefix=API_PREFIX)
app.include_router(notifications_router, prefix=API_PREFIX)
app.include_router(skills_router, prefix=API_PREFIX)
app.include_router(workers_router, prefix=API_PREFIX)
app.include_router(tasks_router, prefix=API_PREFIX)
# 第三方开发者开放注册接口（静态令牌鉴权，与用户 JWT 体系隔离）
app.include_router(open_api_router, prefix=API_PREFIX)


@app.get("/health")
async def health() -> dict[str, str]:
    """存活探针：不依赖数据库，纯进程存活。"""
    return {"status": "ok"}
