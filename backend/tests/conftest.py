"""pytest 全局 fixture — 异步测试 DB + 客户端 + 种子数据

使用 SQLite in-memory + aiosqlite 作为测试 DB，避免依赖 PostgreSQL。
注意：生产用 PostgreSQL，测试用 SQLite，部分 PG 专属特性（JSONB/ARRAY/RULE）
在测试中通过 SQLAlchemy 的 JSON/Unicode 兼容方式模拟。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import AsyncGenerator

# 把 backend/ 加入 sys.path，让 from shared.xxx 可解析
BACKEND_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BACKEND_DIR))

# 测试环境变量 — 必须在导入 shared.config 之前设置
os.environ.setdefault("TESTING", "true")
os.environ.setdefault("JWT_SECRET", "test-secret-key-for-ci")
os.environ.setdefault("JWT_ACCESS_TTL_MINUTES", "15")
os.environ.setdefault("JWT_REFRESH_TTL_DAYS", "7")
os.environ.setdefault("SESSION_TIMEOUT_MINUTES", "60")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_PORT", "5432")
os.environ.setdefault("POSTGRES_USER", "test")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("POSTGRES_DB", "test_db")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import JSON, select
from sqlalchemy.pool import StaticPool

# ============================================================
# SQLite 测试兼容 —— patch PostgreSQL 专属类型
# ============================================================
# 在导入 shared.models 之前，patch JSONB→JSON, ARRAY→String
# 这样 SQLite 能创建表结构（牺牲 PG 专属特性，但应用层逻辑不变）
import sqlalchemy.dialects.postgresql as pg_dialect

class _JSONBCompat(JSON):
    """JSONB 兼容 SQLite —— 退化为 JSON（SQLite 用 TEXT 存储）"""
    pass

class _ARRAYCompat(JSON):
    """ARRAY 兼容 SQLite —— 退化为 JSON（存 list）"""
    def __init__(self, item_type=None, *args, **kwargs):
        super().__init__(*args, **kwargs)

# Monkey-patch —— 必须在 import shared.models 之前
pg_dialect.JSONB = _JSONBCompat
pg_dialect.ARRAY = _ARRAYCompat

# 同时 patch sqlalchemy.dialects.postgresql 模块中的引用
import sqlalchemy as _sa
_sa.dialects.postgresql.JSONB = _JSONBCompat
_sa.dialects.postgresql.ARRAY = _ARRAYCompat

from shared.database import Base, get_db
from shared.models import User
from shared.security import hash_password

# 测试模式下禁用限流 —— 避免 10/min 登录上限卡住批量测试
from shared.rate_limit import limiter as _global_limiter
_global_limiter.enabled = False


# ============================================================
# 测试用 SQLite 引擎 + Session
# ============================================================
@pytest_asyncio.fixture(scope="function")
async def test_engine():
    """每个测试函数独立内存 SQLite —— 隔离彻底"""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,  # in-memory 必须用 StaticPool 共享连接
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def test_db(test_engine) -> AsyncGenerator[AsyncSession, None]:
    """测试 DB Session —— 自动回滚保持隔离"""
    session_factory = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    async with session_factory() as session:
        try:
            yield session
            await session.rollback()
        except Exception:
            await session.rollback()
            raise


@pytest_asyncio.fixture(scope="function")
async def seed_db(test_db: AsyncSession):
    """在测试 DB 中创建种子用户 —— 用于认证测试"""
    users = [
        User(
            id="U-LC",
            email="lisa.chen@biopharma.com",
            name="Lisa Chen",
            role="investigator",
            dept="微生物",
            title="调查员",
            password_hash=hash_password("rca-demo-2025"),
            is_active=True,
        ),
        User(
            id="U-JW",
            email="james.wilson@biopharma.com",
            name="James Wilson",
            role="qa_reviewer",
            dept="质量保证",
            title="QA 审核员",
            password_hash=hash_password("rca-demo-2025"),
            is_active=True,
        ),
        User(
            id="U-ADMIN",
            email="admin@biopharma.com",
            name="System Admin",
            role="admin",
            dept="IT",
            title="管理员",
            password_hash=hash_password("rca-admin-2025"),
            is_active=True,
        ),
    ]
    for u in users:
        test_db.add(u)
    await test_db.commit()
    return users


# ============================================================
# FastAPI TestClient（基于 httpx AsyncClient）
# ============================================================
@pytest_asyncio.fixture(scope="function")
async def app_client(test_engine, seed_db):
    """创建带测试 DB 注入的 FastAPI 测试客户端（auth_service）"""
    from shared.service_base import create_app
    from auth_service.routes import router as auth_router

    app = create_app("auth-service", enable_metrics=False)
    app.include_router(auth_router, prefix="/api/v1")

    # 用测试 Session 工厂覆盖 get_db 依赖
    session_factory = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )

    async def override_get_db():
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()


@pytest_asyncio.fixture(scope="function")
async def case_app_client(test_engine, seed_db):
    """case_service 测试客户端 —— 复用 auth token 验证"""
    from shared.service_base import create_app
    from case_service.routes import router as case_router

    app = create_app("case-service", enable_metrics=False)
    app.include_router(case_router, prefix="/api/v1")

    session_factory = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )

    async def override_get_db():
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    # 同时覆盖 get_db 和 get_db_from_auth（后者依赖 AuthContext）
    from shared.auth_middleware import get_db_from_auth, get_auth_context

    async def override_auth_context(
        authorization=None, request=None, db=None
    ):
        # 简化：用 seed 用户构造 AuthContext，绕过 token 验证
        from shared.auth_middleware import AuthContext
        result = await db.execute(select(User).where(User.email == "lisa.chen@biopharma.com"))
        user = result.scalar_one_or_none()
        return AuthContext(
            user=user,
            session_id="test-session",
            ip_address="127.0.0.1",
            permissions={"case:read", "case:write", "hitl:accept", "sign:approve"},
            db=db if isinstance(db, AsyncSession) else None,
        )

    app.dependency_overrides[get_db] = override_get_db
    # 注意：get_db_from_auth 依赖 get_auth_context，需一并覆盖
    app.dependency_overrides[get_db_from_auth] = override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()


@pytest_asyncio.fixture(scope="function")
async def auth_token(app_client: AsyncClient) -> str:
    """获取 access token —— 用于需要认证的测试"""
    resp = await app_client.post(
        "/api/v1/auth/login",
        json={"email": "lisa.chen@biopharma.com", "password": "rca-demo-2025"},
    )
    assert resp.status_code == 200, f"登录失败: {resp.text}"
    return resp.json()["access_token"]


@pytest.fixture
def auth_headers(auth_token: str) -> dict:
    """认证请求头"""
    return {"Authorization": f"Bearer {auth_token}"}


@pytest.fixture(scope="session")
def event_loop():
    """全局事件循环 —— pytest-asyncio"""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
