"""Auth Service 路由 — 登录/刷新/当前用户"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.audit import append_audit_log
from shared.auth_middleware import AuthContext, get_auth_context, get_db_from_auth
from shared.database import get_db
from shared.exceptions import AuthError
from shared.rate_limit import login_limit, refresh_limit
from shared.models import User
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
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])

# 登录失败上限
MAX_FAILED_ATTEMPTS = 5


@router.post("/login", response_model=TokenResponse, summary="邮箱密码登录")
@login_limit
async def login(
    body: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """用户登录 — 校验密码后签发 access + refresh token

    - 失败 5 次锁定账号
    - 成功后重置失败计数并记录 last_login_at
    - 写入 LOGIN_SUCCESS 审计日志
    """
    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()

    ip = request.client.host if request.client else "unknown"

    # 用户不存在 — 统一错误信息防止枚举
    if not user:
        await append_audit_log(
            db,
            operator=body.email,
            action="LOGIN_FAILED",
            target=None,
            detail=f"用户不存在 (ip={ip})",
            ip_address=ip,
        )
        raise AuthError("邮箱或密码错误")

    if not user.is_active:
        raise AuthError("账号已禁用，请联系管理员")

    if user.failed_login_attempts >= MAX_FAILED_ATTEMPTS:
        raise AuthError("登录失败次数过多，账号已锁定")

    if not verify_password(body.password, user.password_hash):
        user.failed_login_attempts += 1
        await append_audit_log(
            db,
            operator=user.email,
            action="LOGIN_FAILED",
            target=user.id,
            detail=f"密码错误 (第 {user.failed_login_attempts} 次)",
            ip_address=ip,
        )
        # 失败计数必须先提交，否则 AuthError 触发回滚会导致计数丢失
        await db.commit()
        raise AuthError("邮箱或密码错误")

    # 登录成功
    user.failed_login_attempts = 0
    user.last_login_at = datetime.now(timezone.utc)

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
    )

    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        expires_in=900,  # 15min
        user=UserOut.model_validate(user),
    )


@router.post("/refresh", response_model=TokenResponse, summary="刷新 access token")
@refresh_limit
async def refresh(
    request: Request,
    body: RefreshRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """用 refresh token 换取新的 access token"""
    payload = decode_token(body.refresh_token, expected_type="refresh")
    user_id = payload.get("sub")
    if not user_id:
        raise AuthError("Refresh token 缺少 sub")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise AuthError("用户不存在或已禁用")

    access = create_access_token(
        subject=user.id,
        claims={
            "email": user.email,
            "name": user.name,
            "role": user.role,
        },
    )
    new_refresh = create_refresh_token(subject=user.id)

    return TokenResponse(
        access_token=access,
        refresh_token=new_refresh,
        expires_in=900,
        user=UserOut.model_validate(user),
    )


@router.get("/me", response_model=UserOut, summary="当前用户信息")
async def me(auth: AuthContext = Depends(get_auth_context)) -> UserOut:
    """返回当前登录用户信息（基于 access token）"""
    return UserOut.model_validate(auth.user)


@router.post("/logout", summary="登出")
async def logout(
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db_from_auth),
) -> dict:
    """登出 — 记录审计日志

    注意：JWT 是无状态的，真正的 token 失效需要在 Redis 黑名单中登记。
    Phase 1 仅记录日志；Phase 2 加入 Redis token 黑名单。
    """
    await append_audit_log(
        db,
        operator=auth.email,
        action="LOGOUT",
        target=auth.user_id,
        detail=f"{auth.user.name} 登出",
        ip_address=auth.ip_address,
        session_id=auth.session_id,
    )
    return {"status": "ok", "message": "已登出"}
