"""安全模块 — JWT 签发/验证 + 密码哈希 + RBAC"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

import bcrypt
import jwt

from .config import settings
from .exceptions import AuthError, ForbiddenError
from .metrics import JTI_FAILOPEN_TOTAL

logger = logging.getLogger(__name__)

# ---- 密码哈希 ----
# 直接使用 bcrypt（绕过 passlib 1.7.4 与 bcrypt 4.x 的兼容问题）
# bcrypt 限制密码 ≤72 字节：新哈希对超长密码直接拒绝（截断会静默削减熵），
# 旧哈希校验时仍按截断处理以保持兼容。
_BCRYPT_MAX_BYTES = 72


def _truncate(password: str) -> bytes:
    """bcrypt 限制密码长度 ≤72 字节，超长截断（仅用于校验旧哈希）"""
    raw = password.encode("utf-8")
    return raw[:_BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    """bcrypt 哈希 — 返回字符串形式（含算法/成本/盐/哈希）

    超过 bcrypt 72 字节限制的密码直接拒绝，避免静默截断降低熵。
    """
    if len(password.encode("utf-8")) > _BCRYPT_MAX_BYTES:
        raise ValueError("密码长度超过 72 字节限制，请使用更短的密码")
    salt = bcrypt.gensalt(rounds=settings.bcrypt_cost)
    hashed = bcrypt.hashpw(_truncate(password), salt)
    return hashed.decode("utf-8")


def verify_password(plain: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(_truncate(plain), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# 登录"用户不存在"分支的伪校验哈希 —— 首次使用时真实生成一次（保证结构合法），
# 之后只校验固定占位串，让"存在/不存在"两条路径都走一遍完整 bcrypt，
# 消除时间差，缓解账号枚举的时序侧信道。
_DUMMY_HASH: str | None = None


def _get_dummy_hash() -> str:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = bcrypt.hashpw(
            b"rca-timing-equalizer-secret",
            bcrypt.gensalt(rounds=settings.bcrypt_cost),
        ).decode("utf-8")
    return _DUMMY_HASH


def dummy_password_check(plain: str) -> None:
    """对占位哈希执行一次真实 bcrypt 校验，仅用于对齐响应时间，结果被丢弃"""
    verify_password(plain, _get_dummy_hash())


# ---- JWT ----
def create_access_token(
    subject: str,
    claims: dict[str, Any],
    expires_minutes: int | None = None,
) -> str:
    now = datetime.now(UTC)
    exp = now + timedelta(minutes=expires_minutes or settings.jwt_access_ttl_minutes)
    payload = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "type": "access",
        # jti：会话标识。审计日志 session_id、登出吊销（黑名单）都依赖它
        "jti": secrets.token_urlsafe(16),
        **claims,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_refresh_token(subject: str) -> str:
    now = datetime.now(UTC)
    exp = now + timedelta(days=settings.jwt_refresh_ttl_days)
    payload = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "type": "refresh",
        "jti": secrets.token_urlsafe(16),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str, expected_type: Literal["access", "refresh"] = "access") -> dict[str, Any]:
    from .error_codes import ErrorCode

    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.ExpiredSignatureError:
        raise AuthError("Token 已过期，请重新登录", error_code=ErrorCode.TOKEN_EXPIRED.value) from None
    except jwt.InvalidTokenError as e:
        raise AuthError(f"Token 无效: {e}", error_code=ErrorCode.TOKEN_INVALID.value) from e

    # 强制必需声明：jti（会话标识/吊销依据）、sub（主体）、iat（签发时间）。
    # 缺失或为空的 token 一律视为无效 —— 否则调用方 `if jti and is_jti_revoked(jti)`
    # 会因空 jti 跳过吊销检查（吊销"放空"），已登出/被禁用的会话仍能通过校验。
    for claim in ("jti", "sub", "iat"):
        if payload.get(claim) in (None, ""):
            raise AuthError(
                f"Token 缺少必需声明 '{claim}'",
                error_code=ErrorCode.TOKEN_INVALID.value,
            )

    if payload.get("type") != expected_type:
        raise AuthError(
            f"Token 类型错误，期望 {expected_type}",
            error_code=ErrorCode.TOKEN_INVALID.value,
        )
    return cast(dict[str, Any], payload)


# ---- RBAC ----
Role = Literal["admin", "investigator", "qa_reviewer", "ops_manager", "auditor", "operator"]

# 权限矩阵：role → 允许的 action 列表
# SoD 说明（21 CFR Part 11 双人签名）：admin 同时持有 sign:submit + sign:approve
# 是小团队的应急兜底设计 —— 双签独立性不在权限矩阵强制，而在 sign_case 端点级强制：
# ① submit → approve 顺序；② 同人双签拒绝；③ 指定 qa_reviewer_id 一致性校验。
ROLE_PERMISSIONS: dict[str, set[str]] = {
    "admin": {
        "case:read", "case:write", "case:delete",
        "evidence:read", "evidence:write",
        "agent:run", "agent:pause", "agent:reset",
        "hitl:accept", "hitl:reject", "hitl:edit",
        "audit:read", "audit:export",
        "sign:submit", "sign:approve",
        "user:manage", "system:configure",
    },
    "investigator": {
        "case:read", "case:write",
        "evidence:read", "evidence:write",
        "agent:run", "agent:pause", "agent:reset",
        "hitl:accept", "hitl:reject", "hitl:edit",
        "audit:read",
        "sign:submit",
    },
    "qa_reviewer": {
        "case:read", "case:write",
        "evidence:read",
        "audit:read", "audit:export",
        "hitl:accept", "hitl:reject",
        "sign:approve",
    },
    "ops_manager": {
        "case:read",
        "evidence:read",
        "audit:read",
    },
    "auditor": {
        "case:read",
        "evidence:read",
        "audit:read", "audit:export",
    },
    "operator": set(),  # 操作员不直接访问 RCA Pilot
}


def check_permission(role: str, permission: str) -> bool:
    """检查角色是否拥有权限"""
    return permission in ROLE_PERMISSIONS.get(role, set())


def require_permission(role: str, permission: str) -> None:
    """无权限则抛 ForbiddenError"""
    if not check_permission(role, permission):
        raise ForbiddenError(
            f"角色 '{role}' 无权限执行 '{permission}'",
            code="FORBIDDEN",
            details={"role": role, "required_permission": permission},
        )


# ---- 工具函数 ----
def safe_compare(a: str | None, b: str | None) -> bool:
    """恒定时间字符串比较 — 防止时序攻击（任一为 None/空时直接不等）"""
    if not a or not b:
        return False
    return hmac.compare_digest(a.encode(), b.encode())


def sha256_hex(data: str | bytes) -> str:
    """计算 SHA-256"""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


# ---- Token 吊销（jti 黑名单）----
# 依赖 Redis；Redis 不可用时降级为"不吊销"（与旧行为一致），避免服务不可用。
# 21 CFR Part 11 要求会话可即时终止，生产环境必须配置 Redis；
# 每次降级放行都计入 rca_jti_failopen_total 供告警（见 deploy/prometheus-alerts.yml）。

async def revoke_jti(jti: str, ttl_seconds: int) -> bool:
    """将 jti 加入黑名单直至其自然过期。返回是否真正写入（False = 降级未生效）"""
    if not jti:
        return False
    from .redis_client import get_redis

    redis = await get_redis()
    if redis is None:
        JTI_FAILOPEN_TOTAL.labels(op="revoke").inc()
        logger.warning("jti 吊销降级：Redis 不可用，会话 %s 的吊销未生效", jti)
        return False
    try:
        await redis.setex(f"rca:jti_revoked:{jti}", ttl_seconds, "1")  # type: ignore[attr-defined]
        return True
    except Exception:
        JTI_FAILOPEN_TOTAL.labels(op="revoke").inc()
        logger.warning("jti 吊销降级：Redis 写入失败，会话 %s 的吊销未生效", jti)
        return False


async def is_jti_revoked(jti: str) -> bool:
    """检查 jti 是否已被吊销。Redis 不可用时返回 False（放行，与旧行为一致）"""
    if not jti:
        return False
    from .redis_client import get_redis

    redis = await get_redis()
    if redis is None:
        JTI_FAILOPEN_TOTAL.labels(op="check").inc()
        return False
    try:
        return bool(await redis.exists(f"rca:jti_revoked:{jti}"))  # type: ignore[attr-defined]
    except Exception:
        JTI_FAILOPEN_TOTAL.labels(op="check").inc()
        return False
