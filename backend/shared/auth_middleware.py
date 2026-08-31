"""FastAPI 中间件与依赖注入 — 认证/授权/审计上下文"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC

from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .database import get_db
from .exceptions import AuthError
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


def get_client_ip(request: Request) -> str:
    """统一获取客户端 IP

    仅当 settings.trust_proxy_headers=True（部署在可信反代之后）才解析
    X-Forwarded-For / X-Real-IP；否则一律取直连地址 —— 防止伪造 XFF 绕过限流、
    伪造审计 IP。
    """
    from .config import settings

    if settings.trust_proxy_headers:
        # 反代追加模式：XFF 最右侧是最近一跳可信代理写入的客户端地址
        xff = request.headers.get("X-Forwarded-For")
        if xff:
            return xff.split(",")[-1].strip()
        xri = request.headers.get("X-Real-IP")
        if xri:
            return xri
    return request.client.host if request.client else "unknown"


async def get_auth_context(
    request: Request,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> AuthContext:
    """从 Authorization Bearer token 解析用户

    此依赖会创建 DB Session 并在请求结束时统一 commit/rollback。
    后续端点应通过 get_db_from_auth 复用同一 session，避免重复获取连接。
    """
    from datetime import datetime, timedelta

    from .config import settings
    from .error_codes import ErrorCode
    from .security import ROLE_PERMISSIONS, is_jti_revoked

    if not authorization or not authorization.startswith("Bearer "):
        raise AuthError("缺少 Authorization 头")
    token = authorization.removeprefix("Bearer ").strip()
    payload = decode_token(token, expected_type="access")

    # 吊销检查 —— 登出/管理员禁用后 access token 立即失效（Redis 黑名单，不可用时降级放行）
    jti = payload.get("jti", "")
    if jti and await is_jti_revoked(jti):
        raise AuthError("会话已注销，请重新登录", error_code=ErrorCode.TOKEN_INVALID.value)

    user_id = payload.get("sub")
    if not user_id:
        raise AuthError("Token 缺少 sub")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise AuthError("用户不存在或已禁用", error_code=ErrorCode.ACCOUNT_DISABLED.value)

    # 会话超时检查
    iat = payload.get("iat")
    if iat:
        issued = datetime.fromtimestamp(int(iat), tz=UTC)
        if datetime.now(UTC) - issued > timedelta(minutes=settings.session_timeout_minutes):
            raise AuthError("会话超时，请重新登录", error_code=ErrorCode.TOKEN_EXPIRED.value)

    permissions = ROLE_PERMISSIONS.get(user.role, set())

    return AuthContext(
        user=user,
        session_id=jti,
        ip_address=get_client_ip(request),
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
