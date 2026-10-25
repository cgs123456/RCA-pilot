"""数据库会话管理 — 异步 SQLAlchemy"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import cast

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import QueuePool

from .config import settings

engine = create_async_engine(
    cast(str, settings.postgres_dsn),
    echo=settings.debug,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
    pool_recycle=1800,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    """所有 ORM 模型的基类"""
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖注入 — 每个请求一个 session"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """非请求上下文使用（如 Agent 后台任务）"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def release_db_connection(db: AsyncSession) -> None:
    """提交并结束当前事务，把连接归还连接池 —— 供长耗时 await（LLM/外部 HTTP）前调用。

    expire_on_commit=False：已加载的 ORM 属性提交后仍可直接读取，不触发 IO；
    之后的 db.execute 会自动开启新事务。调用点必须已完成该轮 prompt 所需的全部
    数据库读取（未加载的 relationship 在提交后懒加载会在 async 下报 MissingGreenlet）。
    """
    if db.in_transaction():
        await db.commit()


# 连接池统计快照（供 Prometheus 采集）
def pool_snapshot() -> dict[str, int | float | None]:
    """获取连接池当前使用情况快照"""
    pool = engine.pool
    if isinstance(pool, QueuePool):
        return {
            "pool_size": pool.size(),
            "checked_in": pool.checkedin(),
            "checked_out": pool.checkedout(),
            "overflow": pool.overflow(),
        }
    return {"pool_size": 0, "checked_in": 0, "checked_out": 0, "overflow": 0}
