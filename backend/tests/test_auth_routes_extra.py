"""auth_service 路由补充测试 — 覆盖 test_auth.py 未触达的分支

聚焦缺口：
- 登录：锁定剩余时间计算（None/naive/aware/过期）、锁定账号直接拒绝、
  永久锁定（account_lockout_minutes=0）、禁用账号、登录成功重置失败计数、
  不存在用户仍落审计日志（防枚举 + 可追溯）
- refresh：旧 refresh token 重放检测（jti 黑名单）、不存在用户、缺少 sub 声明
- logout：携带 refresh token 一并吊销、无效 refresh 容错、session_id 为空、
  吊销成功时审计 detail 反馈"已写入黑名单"
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from auth_service.routes import LogoutRequest, _lock_remaining_seconds
from auth_service.routes import logout as logout_endpoint
from shared.auth_middleware import AuthContext
from shared.config import settings
from shared.models import AuditLog, User
from shared.security import create_refresh_token

LISA_EMAIL = "lisa.chen@biopharma.com"
LISA_PASSWORD = "rca-demo-2025"


# ============================================================
# _lock_remaining_seconds —— 锁定剩余时间计算
# ============================================================
class TestLockRemainingSeconds:
    def test_no_lock_returns_zero(self):
        assert _lock_remaining_seconds(SimpleNamespace(locked_until=None)) == 0  # type: ignore[arg-type]

    def test_naive_future_datetime_treated_as_utc(self):
        """SQLite 读回的 naive datetime 统一按 UTC 处理"""
        locked = (datetime.now(UTC) + timedelta(minutes=10)).replace(tzinfo=None)
        remaining = _lock_remaining_seconds(SimpleNamespace(locked_until=locked))  # type: ignore[arg-type]
        assert 0 < remaining <= 600

    def test_naive_expired_lock_returns_zero(self):
        locked = (datetime.now(UTC) - timedelta(minutes=10)).replace(tzinfo=None)
        assert _lock_remaining_seconds(SimpleNamespace(locked_until=locked)) == 0  # type: ignore[arg-type]

    def test_aware_future_datetime(self):
        locked = datetime.now(UTC) + timedelta(minutes=5)
        assert 0 < _lock_remaining_seconds(SimpleNamespace(locked_until=locked)) <= 300  # type: ignore[arg-type]


# ============================================================
# 登录端点补充分支
# ============================================================
class TestLoginExtra:
    async def test_nonexistent_user_writes_audit_log(self, app_client, test_db, monkeypatch):
        """不存在用户：401 统一文案 + LOGIN_FAILED 审计落库（防枚举但可追溯）"""
        monkeypatch.setattr("auth_service.routes.dummy_password_check", lambda plain: None)
        resp = await app_client.post(
            "/api/v1/auth/login",
            json={"email": "ghost.user@biopharma.com", "password": "whatever-password"},
        )
        assert resp.status_code == 401
        body = resp.json()
        assert body["code"] == "E4010"
        assert "邮箱或密码错误" in body["message"]

        result = await test_db.execute(
            select(AuditLog).where(AuditLog.operator == "ghost.user@biopharma.com")
        )
        logs = result.scalars().all()
        assert any(log.action == "LOGIN_FAILED" and "用户不存在" in (log.detail or "") for log in logs)

    async def test_inactive_user_rejected(self, app_client, test_db, seed_db):
        """禁用账号：401 + ACCOUNT_DISABLED 错误码"""
        seed_db[0].is_active = False
        await test_db.commit()

        resp = await app_client.post(
            "/api/v1/auth/login", json={"email": LISA_EMAIL, "password": LISA_PASSWORD}
        )
        assert resp.status_code == 401
        body = resp.json()
        assert body["code"] == "E4014"
        assert "禁用" in body["message"]

    async def test_locked_user_rejected_with_retry_after(self, app_client, test_db, seed_db, monkeypatch):
        """已锁定账号：即使密码正确也拒绝，并返回剩余秒数"""
        monkeypatch.setattr("auth_service.routes.verify_password", lambda plain, hashed: True)
        seed_db[0].locked_until = datetime.now(UTC) + timedelta(minutes=10)
        await test_db.commit()

        resp = await app_client.post(
            "/api/v1/auth/login", json={"email": LISA_EMAIL, "password": LISA_PASSWORD}
        )
        assert resp.status_code == 401
        body = resp.json()
        assert body["code"] == "E4013"
        assert "锁定" in body["message"]
        assert body["detail"]["retry_after_seconds"] > 0

    async def test_success_resets_failure_counters(self, app_client, test_db, seed_db, monkeypatch):
        """登录成功后清零失败计数、解除过期锁定、记录 last_login_at"""
        monkeypatch.setattr("auth_service.routes.verify_password", lambda plain, hashed: True)
        user = seed_db[0]
        user.failed_login_attempts = 4
        user.locked_until = datetime.now(UTC) - timedelta(minutes=1)  # 过期锁定
        await test_db.commit()

        resp = await app_client.post(
            "/api/v1/auth/login", json={"email": LISA_EMAIL, "password": LISA_PASSWORD}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["token_type"] == "bearer"
        assert data["expires_in"] == settings.jwt_access_ttl_minutes * 60
        assert data["user"]["email"] == LISA_EMAIL

        # populate_existing：绕过 test_db 会话的 identity map 缓存，读取 app 会话提交后的最新值
        row = (
            await test_db.execute(
                select(User)
                .where(User.email == LISA_EMAIL)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        assert row.failed_login_attempts == 0
        assert row.locked_until is None
        assert row.last_login_at is not None

    async def test_permanent_lock_when_lockout_zero(self, app_client, test_db, seed_db, monkeypatch):
        """account_lockout_minutes=0 → 永久锁定，正确密码也无法解锁"""
        monkeypatch.setattr("auth_service.routes.verify_password", lambda plain, hashed: False)
        monkeypatch.setattr(settings, "account_lockout_minutes", 0)

        for i in range(5):
            resp = await app_client.post(
                "/api/v1/auth/login", json={"email": LISA_EMAIL, "password": "wrong-password-xx"}
            )
            assert resp.status_code == 401
            if i < 4:
                assert resp.json()["code"] == "E4010"  # 普通密码错误
            else:
                assert resp.json()["code"] == "E4013"  # 第 5 次触发锁定

        # 密码正确也拒绝（永久锁定）
        monkeypatch.setattr("auth_service.routes.verify_password", lambda plain, hashed: True)
        resp = await app_client.post(
            "/api/v1/auth/login", json={"email": LISA_EMAIL, "password": LISA_PASSWORD}
        )
        assert resp.status_code == 401
        assert resp.json()["code"] == "E4013"

        row = (
            await test_db.execute(
                select(User)
                .where(User.email == LISA_EMAIL)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        assert row.locked_until is not None
        assert row.locked_until.year == 9999  # datetime.max = 永久


# ============================================================
# refresh 端点补充分支
# ============================================================
class TestRefreshExtra:
    async def _login(self, app_client, monkeypatch) -> dict:
        monkeypatch.setattr("auth_service.routes.verify_password", lambda plain, hashed: True)
        resp = await app_client.post(
            "/api/v1/auth/login", json={"email": LISA_EMAIL, "password": LISA_PASSWORD}
        )
        assert resp.status_code == 200
        return resp.json()  # type: ignore[no-any-return]

    async def test_refresh_reuse_detected(self, app_client, monkeypatch):
        """已轮换（jti 已吊销）的 refresh token 重放 → 401 TOKEN_INVALID"""
        tokens = await self._login(app_client, monkeypatch)
        monkeypatch.setattr(
            "auth_service.routes.is_jti_revoked", AsyncMock(return_value=True)
        )
        resp = await app_client.post(
            "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
        assert resp.status_code == 401
        body = resp.json()
        assert body["code"] == "E4012"
        assert "失效" in body["message"]

    async def test_refresh_unknown_user(self, app_client):
        """refresh token 合法但用户不存在 → 401"""
        token = create_refresh_token("U-GHOST-404")
        resp = await app_client.post("/api/v1/auth/refresh", json={"refresh_token": token})
        assert resp.status_code == 401
        body = resp.json()
        assert body["code"] == "E4014"
        assert "用户不存在或已禁用" in body["message"]

    async def test_refresh_missing_sub_rejected(self, app_client, monkeypatch):
        """payload 缺少 sub 声明 → 401（decode_token 被绕过时的兜底）"""

        def fake_decode(token, expected_type="access"):
            return {"jti": "j-missing-sub", "type": "refresh"}

        monkeypatch.setattr("auth_service.routes.decode_token", fake_decode)
        resp = await app_client.post("/api/v1/auth/refresh", json={"refresh_token": "x.y.z"})
        assert resp.status_code == 401
        assert "缺少 sub" in resp.json()["message"]

    async def test_refresh_success_rotates_tokens(self, app_client, monkeypatch):
        """正常轮换：返回新双 token，且新 access token 可访问 /auth/me"""
        tokens = await self._login(app_client, monkeypatch)

        resp = await app_client.post(
            "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["access_token"]
        assert data["refresh_token"]
        assert data["user"]["email"] == LISA_EMAIL

        me = await app_client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {data['access_token']}"}
        )
        assert me.status_code == 200
        assert me.json()["email"] == LISA_EMAIL


# ============================================================
# logout 端点补充分支
# ============================================================
class TestLogoutExtra:
    async def _login(self, app_client, monkeypatch) -> dict:
        monkeypatch.setattr("auth_service.routes.verify_password", lambda plain, hashed: True)
        resp = await app_client.post(
            "/api/v1/auth/login", json={"email": LISA_EMAIL, "password": LISA_PASSWORD}
        )
        assert resp.status_code == 200
        return resp.json()  # type: ignore[no-any-return]

    async def test_logout_with_refresh_token_revoked(self, app_client, monkeypatch):
        """登出携带 refresh token → 一并解析吊销（Redis 降级也不报错）"""
        tokens = await self._login(app_client, monkeypatch)
        resp = await app_client.post(
            "/api/v1/auth/logout",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
            json={"refresh_token": tokens["refresh_token"]},
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "message": "已登出"}

    async def test_logout_with_invalid_refresh_token_tolerated(self, app_client, auth_headers):
        """无效 refresh token 登出 → 忽略吊销，仍返回 200"""
        resp = await app_client.post(
            "/api/v1/auth/logout", headers=auth_headers, json={"refresh_token": "not-a-jwt"}
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    async def test_logout_revoked_reflected_in_audit(self, app_client, test_db, auth_headers, monkeypatch):
        """吊销成功（revoke_jti=True）时审计 detail 记录"已写入黑名单" """
        monkeypatch.setattr("auth_service.routes.revoke_jti", AsyncMock(return_value=True))
        resp = await app_client.post("/api/v1/auth/logout", headers=auth_headers)
        assert resp.status_code == 200

        result = await test_db.execute(select(AuditLog).where(AuditLog.action == "LOGOUT"))
        logs = result.scalars().all()
        assert any("已写入黑名单" in (log.detail or "") for log in logs)

    async def test_logout_empty_session_id_direct_call(self, test_db, seed_db):
        """session_id 为空（无法吊销）+ body 为 None 的直接调用分支"""
        user = seed_db[0]
        ctx = AuthContext(
            user=user,
            session_id="",
            ip_address="127.0.0.1",
            permissions=set(),
            db=test_db,
        )
        result = await logout_endpoint(auth=ctx, db=test_db, body=None)
        assert result == {"status": "ok", "message": "已登出"}

    async def test_logout_body_without_refresh_token(self, test_db, seed_db):
        """body 有值但 refresh_token 为 None → 跳过吊销分支"""
        user = seed_db[0]
        ctx = AuthContext(
            user=user,
            session_id="session-x",
            ip_address="127.0.0.1",
            permissions=set(),
            db=test_db,
        )
        result = await logout_endpoint(auth=ctx, db=test_db, body=LogoutRequest(refresh_token=None))
        assert result["status"] == "ok"


# ============================================================
# /auth/me —— 补充 token 失效场景
# ============================================================
class TestMeExtra:
    async def test_me_with_malformed_token(self, app_client: AsyncClient):
        """非 JWT 格式的 token → 401 TOKEN_INVALID"""
        resp = await app_client.get(
            "/api/v1/auth/me", headers={"Authorization": "Bearer malformed-token"}
        )
        assert resp.status_code == 401
        assert resp.json()["code"] == "E4012"

    async def test_me_with_wrong_token_type(self, app_client: AsyncClient):
        """用 refresh token 访问受保护端点 → 类型不匹配 → 401"""
        token = create_refresh_token("U-LC")
        resp = await app_client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 401
        assert "Token 类型错误" in resp.json()["message"]
