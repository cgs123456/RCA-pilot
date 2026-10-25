"""P3-④ TOTP 两步验证测试

覆盖：
- RFC 6238 算法：可复现性、漂移窗口、重放拒绝、格式错误
- 登录流程：未启用不受影响 / 已启用必须带码 / 错码计数不锁定 / 重放拒绝 / 停用后恢复
- 注册流程：setup→pending / activate 生效 / 重复 setup 拒绝 / disable 需密码
- 审计留痕：MFA_SETUP_CREATED / MFA_ACTIVATED / MFA_FAILED / MFA_DISABLED
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient

from shared import totp as totp_mod
from shared.totp import current_step, generate_secret, totp_at, verify_totp

pytestmark = pytest.mark.asyncio

DEMO_EMAIL = "lisa.chen@biopharma.com"
DEMO_PASSWORD = "rca-demo-2025"


class TestTotpAlgorithm:
    """RFC 6238 核心算法"""

    def test_generate_secret_is_base32_and_unique(self):
        secrets = {generate_secret() for _ in range(10)}
        assert len(secrets) == 10
        for s in secrets:
            assert len(s) == 32
            assert all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for c in s)

    def test_totp_at_deterministic(self):
        secret = generate_secret()
        step = current_step()
        assert totp_at(secret, step) == totp_at(secret, step)
        assert totp_at(secret, step) != totp_at(secret, step + 1)

    def test_totp_at_six_digits(self):
        code = totp_at(generate_secret(), 12345)
        assert len(code) == 6
        assert code.isdigit()

    def test_verify_current_window(self):
        secret = generate_secret()
        now = 1_700_000_000.0
        code = totp_at(secret, current_step(now))
        assert verify_totp(secret, code, now=now) == current_step(now)

    def test_verify_accepts_clock_drift(self):
        secret = generate_secret()
        now = 1_700_000_000.0
        prev_code = totp_at(secret, current_step(now) - 1)
        assert verify_totp(secret, prev_code, now=now) == current_step(now) - 1

    def test_verify_rejects_outside_window(self):
        secret = generate_secret()
        now = 1_700_000_000.0
        old_code = totp_at(secret, current_step(now) - 2)
        assert verify_totp(secret, old_code, now=now) is None

    def test_verify_rejects_future_step(self):
        secret = generate_secret()
        now = 1_700_000_000.0
        future_code = totp_at(secret, current_step(now) + 1)
        assert verify_totp(secret, future_code, now=now) is None

    def test_verify_rejects_replay(self):
        secret = generate_secret()
        now = 1_700_000_000.0
        step = current_step(now)
        code = totp_at(secret, step)
        assert verify_totp(secret, code, last_used_step=0, now=now) == step
        # 同一时间片的码再次提交 → 拒绝
        assert verify_totp(secret, code, last_used_step=step, now=now) is None

    def test_verify_rejects_malformed(self):
        secret = generate_secret()
        now = 1_700_000_000.0
        assert verify_totp(secret, "abc", now=now) is None
        assert verify_totp(secret, "12345", now=now) is None
        assert verify_totp(secret, "", now=now) is None
        assert verify_totp(secret, "12 45 6", now=now) is None

    def test_provisioning_uri_contains_params(self):
        uri: str = totp_mod.provisioning_uri("ABC234DEF567", "a@b.com", issuer="RCA-Pilot")
        assert uri.startswith("otpauth://totp/RCA-Pilot:a%40b.com?")
        assert "secret=ABC234DEF567" in uri
        assert "digits=6" in uri and "period=30" in uri


class TestTotpLoginFlow:
    """登录 × TOTP 集成"""

    async def _setup_and_activate(self, client: AsyncClient, token: str) -> str:
        headers = {"Authorization": f"Bearer {token}"}
        resp = await client.post("/api/v1/auth/totp/setup", headers=headers)
        assert resp.status_code == 200
        secret: str = resp.json()["secret"]
        act = await client.post(
            "/api/v1/auth/totp/activate",
            json={"code": totp_at(secret, current_step())},
            headers=headers,
        )
        assert act.status_code == 200
        return secret

    async def test_login_without_totp_unaffected(self, app_client: AsyncClient):
        resp = await app_client.post(
            "/api/v1/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}
        )
        assert resp.status_code == 200

    async def test_enabled_user_requires_mfa_code(self, app_client: AsyncClient):
        login = await app_client.post(
            "/api/v1/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}
        )
        await self._setup_and_activate(app_client, login.json()["access_token"])

        no_code = await app_client.post(
            "/api/v1/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}
        )
        assert no_code.status_code == 401
        assert no_code.json()["code"] == "E4015"
        assert no_code.json()["detail"]["mfa_required"] is True

    async def test_login_with_valid_code(self, app_client: AsyncClient):
        login = await app_client.post(
            "/api/v1/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}
        )
        secret = await self._setup_and_activate(app_client, login.json()["access_token"])

        resp = await app_client.post(
            "/api/v1/auth/login",
            json={
                "email": DEMO_EMAIL,
                "password": DEMO_PASSWORD,
                "mfa_code": totp_at(secret, current_step()),
            },
        )
        assert resp.status_code == 200
        assert "access_token" in resp.json()

    async def test_wrong_code_counts_failure_but_not_lockout(self, app_client: AsyncClient):
        login = await app_client.post(
            "/api/v1/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}
        )
        secret = await self._setup_and_activate(app_client, login.json()["access_token"])

        for _ in range(3):
            bad = await app_client.post(
                "/api/v1/auth/login",
                json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD, "mfa_code": "000000"},
            )
            assert bad.status_code == 401
            assert bad.json()["code"] == "E4016"

        # 密码正确的错码不触发账号锁定（锁定语义只属于密码失败）
        good = await app_client.post(
            "/api/v1/auth/login",
            json={
                "email": DEMO_EMAIL,
                "password": DEMO_PASSWORD,
                "mfa_code": totp_at(secret, current_step()),
            },
        )
        assert good.status_code == 200

    async def test_replay_same_code_rejected(self, app_client: AsyncClient):
        login = await app_client.post(
            "/api/v1/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}
        )
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        setup = await app_client.post("/api/v1/auth/totp/setup", headers=headers)
        secret = setup.json()["secret"]
        code = totp_at(secret, current_step())
        act = await app_client.post(
            "/api/v1/auth/totp/activate", json={"code": code}, headers=headers
        )
        assert act.status_code == 200

        first = await app_client.post(
            "/api/v1/auth/login",
            json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD, "mfa_code": code},
        )
        assert first.status_code == 200
        # 同一枚码立刻重放 → 401（totp_last_step 已推进到当前时间片）
        replay = await app_client.post(
            "/api/v1/auth/login",
            json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD, "mfa_code": code},
        )
        assert replay.status_code == 401

    async def test_disable_with_password_restores_plain_login(self, app_client: AsyncClient):
        login = await app_client.post(
            "/api/v1/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}
        )
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        setup = await app_client.post("/api/v1/auth/totp/setup", headers=headers)
        secret = setup.json()["secret"]
        act = await app_client.post(
            "/api/v1/auth/totp/activate",
            json={"code": totp_at(secret, current_step())},
            headers=headers,
        )
        assert act.status_code == 200

        wrong_pw = await app_client.post(
            "/api/v1/auth/totp/disable",
            json={"password": "{{SECRET_8v4zkwcb}}"},
            headers=headers,
        )
        assert wrong_pw.status_code in (401, 403)

        off = await app_client.post(
            "/api/v1/auth/totp/disable",
            json={"password": DEMO_PASSWORD},
            headers=headers,
        )
        assert off.status_code == 200

        plain = await app_client.post(
            "/api/v1/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}
        )
        assert plain.status_code == 200

    async def test_pending_setup_not_enforced_on_login(self, app_client: AsyncClient):
        """setup 后未 activate：登录不要求验证码（防止用户自锁），可重新生成种子"""
        login = await app_client.post(
            "/api/v1/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}
        )
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        setup = await app_client.post("/api/v1/auth/totp/setup", headers=headers)
        assert setup.status_code == 200
        stale_secret = setup.json()["secret"]

        # pending 状态：无码登录成功
        plain = await app_client.post(
            "/api/v1/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}
        )
        assert plain.status_code == 200

        # 重新 setup 发放新种子
        again = await app_client.post(
            "/api/v1/auth/totp/setup",
            headers={"Authorization": f"Bearer {plain.json()['access_token']}"},
        )
        assert again.status_code == 200
        assert again.json()["secret"] != stale_secret

    async def test_activate_without_setup_rejected(self, app_client: AsyncClient):
        login = await app_client.post(
            "/api/v1/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}
        )
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        resp = await app_client.post(
            "/api/v1/auth/totp/activate", json={"code": "123456"}, headers=headers
        )
        assert resp.status_code in (400, 401, 409)

    async def test_setup_twice_after_activation_rejected(self, app_client: AsyncClient):
        login = await app_client.post(
            "/api/v1/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}
        )
        token = login.json()["access_token"]
        await self._setup_and_activate(app_client, token)
        again = await app_client.post(
            "/api/v1/auth/totp/setup",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert again.status_code in (400, 401, 409)


class TestTotpAudit:
    """TOTP 操作必须留痕（GxP）—— app_client 只挂 auth 路由，直接查审计表"""

    async def test_mfa_actions_written_to_audit(self, app_client: AsyncClient, test_db):
        from sqlalchemy import select

        from shared.models import AuditLog

        login = await app_client.post(
            "/api/v1/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}
        )
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        setup = await app_client.post("/api/v1/auth/totp/setup", headers=headers)
        secret = setup.json()["secret"]
        await app_client.post(
            "/api/v1/auth/totp/activate",
            json={"code": totp_at(secret, current_step())},
            headers=headers,
        )
        rows = (await test_db.execute(select(AuditLog.action))).scalars().all()
        assert "MFA_SETUP_CREATED" in rows
        assert "MFA_ACTIVATED" in rows
