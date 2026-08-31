"""Alembic 迁移环境 — 异步 + 自动从 ORM 模型生成迁移"""
from __future__ import annotations

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# 把 backend/ 加入 sys.path
BACKEND_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from shared.config import settings  # noqa: E402
from shared.database import Base  # noqa: E402

# 导入所有模型，让 Base.metadata 知道全部表
from shared.models import (  # noqa: E402, F401
    AgentRun,
    AuditLog,
    Deviation,
    Evidence,
    FiveWhy,
    SignatureRecord,
    SkepticChallenge,
    SourceSystem,
    TimelineEvent,
    User,
)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 从 settings 读取数据库 URL，转换为同步 driver 供 Alembic 使用
# Alembic 的 offline 模式用同步 URL
sync_url = settings.postgres_dsn.replace("postgresql+asyncpg://", "postgresql+psycopg2://")
config.set_main_option("sqlalchemy.url", sync_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式 —— 生成 SQL 脚本不连接 DB"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online_async() -> None:
    """在线异步模式 —— 实际连接 DB 执行迁移"""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = settings.postgres_dsn
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """在线模式入口 —— 包装异步调用"""
    asyncio.run(run_migrations_online_async())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
