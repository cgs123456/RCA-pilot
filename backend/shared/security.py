"""安全模块 — JWT 签发/验证 + 密码哈希 + RBAC"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Literal, Optional

import bcrypt
import jwt

from .config import settings
from .exceptions import AuthError, ForbiddenError

# ---- 密码哈希 ----
# 直接使用 bcrypt（绕过 passlib 1.7.4 与 bcrypt 4.x 的兼容问题）
# bcrypt 限制密码 ≤72 字节，对超长密码截断前 72 字节（与 passlib 行为一致）
_BCRYPT_MAX_BYTES = 72


def _truncate(password: str) -> bytes:
    """bcrypt 限制密码长度 ≤72 字节，超长截断（与 passlib 默认行为一致）"""
    raw = password.encode("utf-8")
    return raw[:_BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    """bcrypt 哈希 — 返回字符串形式（含算法/成本/盐/哈希）"""
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(_truncate(password), salt)
    return hashed.decode("utf-8")


def verify_password(plain: str, hashed: Optional[str]) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(_truncate(plain), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ---- JWT ----
def create_access_token(
    subject: str,
    claims: Dict[str, Any],
    expires_minutes: Optional[int] = None,
) -> str:
    now = datetime.now(timezone.utc)
    exp = now + timedelta(minutes=expires_minutes or settings.jwt_access_ttl_minutes)
    payload = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "type": "access",
        **claims,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_refresh_token(subject: str) -> str:
    now = datetime.now(timezone.utc)
    exp = now + timedelta(days=settings.jwt_refresh_ttl_days)
    payload = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "type": "refresh",
        "jti": secrets.token_urlsafe(16),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str, expected_type: Literal["access", "refresh"] = "access") -> Dict[str, Any]:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.ExpiredSignatureError:
        raise AuthError("Token 已过期，请重新登录")
    except jwt.InvalidTokenError as e:
        raise AuthError(f"Token 无效: {e}")

    if payload.get("type") != expected_type:
        raise AuthError(f"Token 类型错误，期望 {expected_type}")
    return payload


# ---- RBAC ----
Role = Literal["admin", "investigator", "qa_reviewer", "ops_manager", "auditor", "operator"]

# 权限矩阵：role → 允许的 action 列表
ROLE_PERMISSIONS: Dict[str, set[str]] = {
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
def safe_compare(a: str, b: str) -> bool:
    """恒定时间字符串比较 — 防止时序攻击"""
    return hmac.compare_digest(a.encode(), b.encode())


def sha256_hex(data: str | bytes) -> str:
    """计算 SHA-256"""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()
