"""Alembic 环境：async engine + 应用 settings 注入 URL。"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import settings

# 导入全部模型，确保 metadata 完整（autogenerate 依赖）
from app.db import models  # noqa: E402, F401
from app.db.base import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata


def include_name(name: str | None, type_: str, parent_names: dict) -> bool:  # noqa: ARG001
    """Autogenerate 过滤：LangGraph checkpointer 的表由运行期自建，不归 ORM/alembic 管。

    不过滤的话 autogenerate 会生成 DROP TABLE checkpoints，把会话检查点全删掉。
    """
    return not (type_ == "table" and name is not None and name.startswith("checkpoint"))


def run_migrations_offline() -> None:
    """离线模式：仅生成 SQL，不连库。"""
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_name=include_name,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection, target_metadata=target_metadata, include_name=include_name
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
