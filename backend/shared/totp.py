"""RFC 6238 TOTP 实现 — 仅用标准库（HMAC-SHA1，RFC 4226 动态截断）

P3-④：为 21 CFR Part 11 登录加第二因子。设计约束：
- 不引入 pyotp/cryptography 等新依赖：TOTP 核心只是 HMAC + 截断，stdlib 足够；
- 种子（base32）落 users.totp_secret —— 数据库已有 RULE 禁 UPDATE/DELETE 审计与
  传输层 TLS 保护；静态加密留待 KMS 集成（见 COU.md 已知限制）；
- 防重放：verify 成功返回命中的时间片，调用方将其写回 users.totp_last_step，
  后续同一（或更早）时间片的码一律拒绝。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time

# RFC 6238 推荐参数
TIMESTEP_SECONDS = 30
DIGITS = 6
# 允许客户端时钟漂移 ±1 个时间片（±30s）
DEFAULT_VALID_WINDOW = 1


def generate_secret() -> str:
    """生成 160 位随机种子，base32 无填充（Google Authenticator 兼容）"""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _decode_secret(secret: str) -> bytes:
    """base32 解码；容忍小写与缺失 padding"""
    normalized = secret.strip().replace(" ", "").upper()
    padding = "=" * (-len(normalized) % 8)
    return base64.b32decode(normalized + padding)


def totp_at(secret: str, step: int, digits: int = DIGITS) -> str:
    """计算指定时间片的 TOTP 码（RFC 4226 动态截断，SHA-1）"""
    key = _decode_secret(secret)
    counter = struct.pack(">Q", step)
    digest = hmac.new(key, counter, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (
        ((digest[offset] & 0x7F) << 24)
        | ((digest[offset + 1] & 0xFF) << 16)
        | ((digest[offset + 2] & 0xFF) << 8)
        | (digest[offset + 3] & 0xFF)
    )
    return f"{code % (10**digits):0{digits}d}"


def current_step(at: float | None = None) -> int:
    """当前时间片序号"""
    return int((at if at is not None else time.time()) // TIMESTEP_SECONDS)


def verify_totp(
    secret: str,
    code: str,
    *,
    valid_window: int = DEFAULT_VALID_WINDOW,
    last_used_step: int = 0,
    now: float | None = None,
) -> int | None:
    """校验 TOTP 码，命中返回其时间片序号，失败返回 None

    - 只接受纯数字、位数正确的码；
    - 只允许 last_used_step 之后的时间片（同窗口重放拒绝）；
    - hmac.compare_digest 常数时间比较，避免逐位猜测。
    """
    code = (code or "").strip()
    if not code.isdigit() or len(code) != DIGITS:
        return None
    now_step = current_step(now)
    for step in range(now_step, now_step - valid_window - 1, -1):
        if step <= last_used_step:
            break
        expected = totp_at(secret, step)
        if hmac.compare_digest(expected, code):
            return step
    return None


def provisioning_uri(secret: str, email: str, issuer: str = "RCA-Pilot") -> str:
    """otpauth:// 统一资源标识 — 前端可直接渲染二维码或用户手输种子"""
    from urllib.parse import quote

    return (
        f"otpauth://totp/{quote(issuer)}:{quote(email)}"
        f"?secret={secret}&issuer={quote(issuer)}"
        f"&algorithm=SHA1&digits={DIGITS}&period={TIMESTEP_SECONDS}"
    )
