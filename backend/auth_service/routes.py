"""Auth Service 路由 — 登录/刷新/当前用户/登出

注意：本文件不使用 `from __future__ import annotations`（PEP 563）。
端点被 slowapi 限流装饰器包装后，FastAPI 会用包装函数的 __globals__
解析字符串注解（解析到 slowapi 模块命名空间导致 NameError）；
真实对象注解可完全绕开该问题。
"""
from datetime import UTC, datetime, timedelta

import anyio.to_thread
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.audit import append_audit_log
from shared.auth_middleware import AuthContext, get_auth_context, get_client_ip, get_db_from_auth
from shared.config import settings
from shared.database import get_db
from shared.error_codes import ErrorCode
from shared.exceptions import AuthError
from shared.models import User
from shared.rate_limit import login_limit, refresh_limit
from shared.schemas import (
    LoginRequest,
    RefreshRequest,
    TokenResponse,
    UserOut,
)
from shared.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    dummy_password_check,
    is_jti_revoked,
    revoke_jti,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])

# 登录失败上限
MAX_FAILED_ATTEMPTS = 5


def _lock_remaining_seconds(user: User) -> int:
    """账号剩余锁定秒数；未锁定返回 0（SQLite 会返回 naive datetime，统一按 UTC 处理）"""
    if user.locked_until is None:
        return 0
    locked_until = user.locked_until
    if locked_until.tzinfo is None:
        locked_until = locked_until.replace(tzinfo=UTC)
    remaining = (locked_until - datetime.now(UTC)).total_seconds()
    return max(0, int(remaining))


@router.post("/login", response_model=TokenResponse, summary="邮箱密码登录")
@login_limit
async def login(
    body: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """用户登录 — 校验密码后签发 access + refresh token

    - 失败 5 次锁定账号（时间窗自动解锁，默认 15 分钟）
    - 成功后重置失败计数并记录 last_login_at
    - 写入 LOGIN_SUCCESS 审计日志
    """
    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()

    ip = get_client_ip(request)

    # 用户不存在 — 先做一次真实 bcrypt 伪校验，对齐"密码错误"分支的响应耗时，
    # 消除账号枚举的时序侧信道；同时提交审计日志（append_audit_log 内部会 flush，
    # 但后续 raise 会触发整个会话回滚，必须先 commit 才能持久化）。
    if not user:
        # 放线程池执行 bcrypt，避免阻塞事件循环
        await anyio.to_thread.run_sync(dummy_password_check, body.password)
        await append_audit_log(
            db,
            operator=body.email,
            action="LOGIN_FAILED",
            target=None,
            detail=f"用户不存在 (ip={ip})",
            ip_address=ip,
        )
        await db.commit()
        raise AuthError("邮箱或密码错误")

    if not user.is_active:
        raise AuthError("账号已禁用，请联系管理员", error_code=ErrorCode.ACCOUNT_DISABLED.value)

    remaining = _lock_remaining_seconds(user)
    if remaining > 0:
        raise AuthError(
            f"登录失败次数过多，账号已锁定（约 {remaining // 60 + 1} 分钟后自动解锁）",
            error_code=ErrorCode.ACCOUNT_LOCKED.value,
            details={"retry_after_seconds": remaining},
        )

    # bcrypt 校验是 CPU 密集（cost 12 约数百 ms），放线程池避免阻塞事件循环
    password_ok = await anyio.to_thread.run_sync(verify_password, body.password, user.password_hash)

    if not password_ok:
        user.failed_login_attempts += 1
        if user.failed_login_attempts >= MAX_FAILED_ATTEMPTS:
            lock_minutes = settings.account_lockout_minutes
            user.locked_until = (
                datetime.now(UTC) + timedelta(minutes=lock_minutes)
                if lock_minutes > 0
                else datetime.max.replace(tzinfo=UTC)  # 0 = 永久锁定
            )
            detail = (
                f"密码错误 (第 {user.failed_login_attempts} 次)，账号锁定 {lock_minutes} 分钟"
                if lock_minutes > 0
                else f"密码错误 (第 {user.failed_login_attempts} 次)，账号永久锁定"
            )
            error_code = ErrorCode.ACCOUNT_LOCKED.value
        else:
            detail = f"密码错误 (第 {user.failed_login_attempts} 次)"
            error_code = None
        await append_audit_log(
            db,
            operator=user.email,
            action="LOGIN_FAILED",
            target=user.id,
            detail=detail,
            ip_address=ip,
        )
        # 失败计数必须先提交，否则 AuthError 触发回滚会导致计数丢失
        await db.commit()
        if error_code:
            raise AuthError("登录失败次数过多，账号已锁定", error_code=error_code)
        raise AuthError("邮箱或密码错误")

    # 登录成功
    user.failed_login_attempts = 0
    user.locked_until = None
    user.last_login_at = datetime.now(UTC)

    access = create_access_token(
        subject=user.id,
        claims={
            "email": user.email,
            "name": user.name,
            "role": user.role,
        },
    )
    refresh = create_refresh_token(subject=user.id)

    await append_audit_log(
        db,
        operator=user.email,
        action="LOGIN_SUCCESS",
        target=user.id,
        detail=f"{user.name} ({user.role}) 登录成功",
        ip_address=ip,
        session_id=decode_token(access).get("jti", ""),
    )

    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.jwt_access_ttl_minutes * 60,
        user=UserOut.model_validate(user),
    )


@router.post("/refresh", response_model=TokenResponse, summary="刷新 access token")
@refresh_limit
async def refresh(
    request: Request,
    body: RefreshRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """用 refresh token 换取新的 access token

    Refresh token 轮换：旧 refresh 的 jti 立即吊销（Redis 黑名单，不可用时降级），
    已使用过的 refresh 再次提交会被拒绝（重放检测）。
    """
    payload = decode_token(body.refresh_token, expected_type="refresh")
    user_id = payload.get("sub")
    jti = payload.get("jti", "")
    if not user_id:
        raise AuthError("Refresh token 缺少 sub")

    # 重放检测：轮换后的旧 token 再来用 → 拒绝
    if jti and await is_jti_revoked(jti):
        await append_audit_log(
            db,
            operator=user_id,
            action="REFRESH_TOKEN_REUSE",
            target=user_id,
            detail="已轮换的 refresh token 被重放",
            ip_address=get_client_ip(request),
        )
        raise AuthError("Refresh token 已失效，请重新登录", error_code=ErrorCode.TOKEN_INVALID.value)

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise AuthError("用户不存在或已禁用", error_code=ErrorCode.ACCOUNT_DISABLED.value)

    access = create_access_token(
        subject=user.id,
        claims={
            "email": user.email,
            "name": user.name,
            "role": user.role,
        },
    )
    new_refresh = create_refresh_token(subject=user.id)

    # 吊销旧 refresh jti —— 从现在到其 exp 时间内不可再用
    exp = payload.get("exp")
    if jti and exp:
        ttl = max(60, int(exp - datetime.now(UTC).timestamp()))
        await revoke_jti(jti, ttl)

    return TokenResponse(
        access_token=access,
        refresh_token=new_refresh,
        expires_in=settings.jwt_access_ttl_minutes * 60,
        user=UserOut.model_validate(user),
    )


class LogoutRequest(BaseModel):
    """可选携带 refresh_token，登出时一并吊销"""
    refresh_token: str | None = None


@router.get("/me", response_model=UserOut, summary="当前用户信息")
async def me(auth: AuthContext = Depends(get_auth_context)) -> UserOut:
    """返回当前登录用户信息（基于 access token）"""
    return UserOut.model_validate(auth.user)


@router.post("/logout", summary="登出")
async def logout(
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db_from_auth),
    body: LogoutRequest | None = None,
) -> dict:
    """登出 — 记录审计日志 + 吊销当前会话

    吊销实现：access token 的 jti 写入 Redis 黑名单（剩余 TTL 内不可再用）；
    可选携带 refresh_token 一并吊销。Redis 不可用时降级为仅记录日志。
    """
    revoked = False
    if auth.session_id:
        revoked = await revoke_jti(auth.session_id, settings.jwt_access_ttl_minutes * 60)
    if body and body.refresh_token:
        try:
            rp = decode_token(body.refresh_token, expected_type="refresh")
            exp = rp.get("exp")
            if rp.get("jti") and exp:
                ttl = max(60, int(exp - datetime.now(UTC).timestamp()))
                await revoke_jti(rp["jti"], ttl)
        except AuthError:
            pass  # 过期/无效的 refresh 无需吊销

    await append_audit_log(
        db,
        operator=auth.email,
        action="LOGOUT",
        target=auth.user_id,
        detail=f"{auth.user.name} 登出（会话吊销: {'已写入黑名单' if revoked else '降级未生效'}）",
        ip_address=auth.ip_address,
        session_id=auth.session_id,
    )
    return {"status": "ok", "message": "已登出"}
