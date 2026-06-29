"""FastAPI 中间件与依赖注入 — 认证/授权/审计上下文"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .database import get_db
from .exceptions import AuthError, ForbiddenError
from .models import User
from .security import decode_token, require_permission


@dataclass
class AuthContext:
    """请求认证上下文 — 注入到每个受保护端点

    携带 db session 以避免每个请求创建多个 DB 连接（Critical #4 修复）。
    端点应使用 `db: AsyncSession = Depends(get_db_from_auth)` 而非 `Depends(get_db)`，
    以复用 AuthContext 已建立的同一 session。
    """
    user: User
    session_id: str
    ip_address: str
    permissions: set[str]
    db: AsyncSession

    @property
    def role(self) -> str:
        return self.user.role

    @property
    def email(self) -> str:
        return self.user.email

    @property
    def user_id(self) -> str:
        return self.user.id

    def check(self, permission: str) -> None:
        require_permission(self.role, permission)


async def get_auth_context(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> AuthContext:
    """从 Authorization Bearer token 解析用户

    此依赖会创建 DB Session 并在请求结束时统一 commit/rollback。
    后续端点应通过 get_db_from_auth 复用同一 session，避免重复获取连接。
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise AuthError("缺少 Authorization 头")
    token = authorization.removeprefix("Bearer ").strip()
    payload = decode_token(token, expected_type="access")

    user_id = payload.get("sub")
    if not user_id:
        raise AuthError("Token 缺少 sub")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise AuthError("用户不存在或已禁用")

    # 会话超时检查
    from .config import settings
    from datetime import datetime, timezone, timedelta
    iat = payload.get("iat")
    if iat:
        issued = datetime.fromtimestamp(iat, tz=timezone.utc)
        if datetime.now(timezone.utc) - issued > timedelta(minutes=settings.session_timeout_minutes):
            raise AuthError("会话超时，请重新登录")

    from .security import ROLE_PERMISSIONS
    permissions = ROLE_PERMISSIONS.get(user.role, set())

    return AuthContext(
        user=user,
        session_id=payload.get("jti", ""),
        ip_address=request.client.host if request.client else "unknown",
        permissions=permissions,
        db=db,
    )


async def get_db_from_auth(ctx: AuthContext = Depends(get_auth_context)) -> AsyncSession:
    """从 AuthContext 复用 DB Session —— 端点使用此依赖避免重复获取连接

    用法：
        async def list_cases(
            db: AsyncSession = Depends(get_db_from_auth),
            auth: AuthContext = Depends(require("case:read")),
        ): ...
    """
    return ctx.db


def require(*permissions: str):
    """权限校验依赖工厂

    用法：
        @router.get("/cases", dependencies=[Depends(require("case:read"))])
        async def list_cases(...): ...

        或在端点内：
        async def endpoint(auth: AuthContext = Depends(get_auth_context)):
            auth.check("case:write")
    """
    async def _checker(ctx: AuthContext = Depends(get_auth_context)) -> AuthContext:
        for perm in permissions:
            ctx.check(perm)
        return ctx
    return _checker
