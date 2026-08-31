"""数据库会话管理 — 异步 SQLAlchemy"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from .config import settings

engine = create_async_engine(
    settings.postgres_dsn,
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


# 连接池统计快照（供 Prometheus 采集）
def pool_snapshot() -> dict[str, int | float | None]:
    """获取连接池当前使用情况快照"""
    if engine.pool is None:
        return {"pool_size": 0, "checked_in": 0, "checked_out": 0, "overflow": 0}
    pool = engine.pool
    return {
        "pool_size": pool.size(),
        "checked_in": pool.checked_in(),
        "checked_out": pool.checked_out(),
        "overflow": pool.overflow() if hasattr(pool, "overflow") else None,
    }
