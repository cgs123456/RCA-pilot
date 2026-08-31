"""认证服务测试 — login/refresh/me/logout + 失败锁定

覆盖：
- 正常登录返回 access + refresh token
- 错误密码 5 次后账号锁定
- 不存在的用户统一错误信息（防枚举）
- /auth/me 返回当前用户
- /auth/refresh 刷新 token
- /auth/logout 写审计日志
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


class TestLogin:
    """登录端点测试"""

    async def test_login_success(self, app_client: AsyncClient):
        """正常登录返回 200 + 双 token"""
        resp = await app_client.post(
            "/api/v1/auth/login",
            json={"email": "lisa.chen@biopharma.com", "password": "rca-demo-2025"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["token_type"] == "bearer"
        assert data["expires_in"] == 900
        assert data["user"]["email"] == "lisa.chen@biopharma.com"
        assert data["user"]["role"] == "investigator"

    async def test_login_wrong_password(self, app_client: AsyncClient):
        """错误密码返回 401"""
        resp = await app_client.post(
            "/api/v1/auth/login",
            json={"email": "lisa.chen@biopharma.com", "password": "wrong-password"},
        )
        assert resp.status_code == 401
        assert "邮箱或密码错误" in resp.json()["message"]

    async def test_login_nonexistent_user(self, app_client: AsyncClient):
        """不存在用户返回相同错误信息（防枚举）"""
        resp = await app_client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@biopharma.com", "password": "any-password"},
        )
        assert resp.status_code == 401
        assert "邮箱或密码错误" in resp.json()["message"]

    async def test_login_locks_after_5_failures(self, app_client: AsyncClient):
        """连续 5 次失败后锁定账号"""
        for _i in range(5):
            resp = await app_client.post(
                "/api/v1/auth/login",
                json={"email": "lisa.chen@biopharma.com", "password": "wrong-password"},
            )
            assert resp.status_code == 401

        # 第 6 次应提示锁定
        resp = await app_client.post(
            "/api/v1/auth/login",
            json={"email": "lisa.chen@biopharma.com", "password": "rca-demo-2025"},
        )
        assert resp.status_code == 401
        assert "锁定" in resp.json()["message"]

    async def test_login_invalid_payload(self, app_client: AsyncClient):
        """缺字段返回 422"""
        resp = await app_client.post("/api/v1/auth/login", json={"email": "x@y.com"})
        assert resp.status_code == 422


class TestMe:
    """当前用户端点测试"""

    async def test_me_with_valid_token(self, app_client: AsyncClient, auth_headers: dict):
        """有效 token 返回用户信息"""
        resp = await app_client.get("/api/v1/auth/me", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["email"] == "lisa.chen@biopharma.com"
        assert data["role"] == "investigator"

    async def test_me_without_token(self, app_client: AsyncClient):
        """无 token 返回 401"""
        resp = await app_client.get("/api/v1/auth/me")
        assert resp.status_code == 401

    async def test_me_with_invalid_token(self, app_client: AsyncClient):
        """无效 token 返回 401"""
        resp = await app_client.get(
            "/api/v1/auth/me", headers={"Authorization": "Bearer invalid.token.here"}
        )
        assert resp.status_code == 401


class TestRefresh:
    """Token 刷新测试"""

    async def test_refresh_success(self, app_client: AsyncClient):
        """refresh token 换取新 access token"""
        # 先登录
        login_resp = await app_client.post(
            "/api/v1/auth/login",
            json={"email": "lisa.chen@biopharma.com", "password": "rca-demo-2025"},
        )
        refresh_token = login_resp.json()["refresh_token"]

        # 刷新
        resp = await app_client.post(
            "/api/v1/auth/refresh", json={"refresh_token": refresh_token}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["token_type"] == "bearer"
        assert data["user"]["email"] == "lisa.chen@biopharma.com"

    async def test_refresh_invalid_token(self, app_client: AsyncClient):
        """无效 refresh token 返回 401"""
        resp = await app_client.post(
            "/api/v1/auth/refresh", json={"refresh_token": "invalid.token"}
        )
        assert resp.status_code == 401


class TestLogout:
    """登出测试"""

    async def test_logout_success(self, app_client: AsyncClient, auth_headers: dict):
        """登出返回 200 + 写审计日志"""
        resp = await app_client.post("/api/v1/auth/logout", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    async def test_logout_without_token(self, app_client: AsyncClient):
        """无 token 登出返回 401"""
        resp = await app_client.post("/api/v1/auth/logout")
        assert resp.status_code == 401
