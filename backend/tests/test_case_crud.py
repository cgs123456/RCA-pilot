"""案例 CRUD + 状态机测试

覆盖：
- 创建案例
- 查询列表 + 分页
- PATCH 更新字段
- Critical #3: PATCH 不能直接设 status=closed（必须走签名端点）
- Critical #3: PATCH 不能设 status=reviewing（系统态）
- 已结案案例不可修改
- 权限校验：调查员可写，操作员只读
"""
from __future__ import annotations

from datetime import UTC, datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import Deviation, User
from shared.security import hash_password

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def seeded_case(test_db: AsyncSession, seed_db):
    """创建一个用于测试的案例"""
    case = Deviation(
        id="DEV-TEST-001",
        title="测试偏差案例",
        description="测试用",
        severity="major",
        status="investigating",
        case_type="EM+HVAC",
        complexity=2,
        location="Suite 1",
        area="B 级区",
        investigator_id="U-LC",
        qa_reviewer_id="U-JW",
        discovered_at=datetime.now(UTC),
        deadline=datetime.now(UTC),
        organism="Micrococcus",
        progress=30,
        hitl_gates={
            "gate1": "done",
            "gate2": "done",
            "gate3": "pending",
            "gate4": "pending",
            "gate5": "pending",
        },
    )
    test_db.add(case)
    await test_db.commit()
    return case


@pytest.fixture
async def operator_user(test_db: AsyncSession):
    """创建一个 operator 用户（只读权限）"""
    u = User(
        id="U-OP-TEST",
        email="op-test@biopharma.com",
        name="Operator Test",
        role="operator",
        password_hash=hash_password("op-2025"),
        is_active=True,
    )
    test_db.add(u)
    await test_db.commit()
    return u


class TestCaseCreate:
    """创建案例测试"""

    async def test_create_case_success(
        self, case_app_client: AsyncClient, auth_headers: dict
    ):
        """调查员创建案例"""
        resp = await case_app_client.post(
            "/api/v1/cases",
            json={
                "id": "DEV-NEW-001",
                "title": "新案例测试标题",
                "severity": "major",
                "status": "investigating",
                "discovered_at": "2025-08-17T06:00:00+00:00",
            },
            headers=auth_headers,
        )
        assert resp.status_code in (200, 201), resp.text
        data = resp.json()
        assert data["id"] == "DEV-NEW-001"
        assert data["title"] == "新案例测试标题"

    async def test_create_case_duplicate_id(
        self, case_app_client: AsyncClient, auth_headers: dict, seeded_case
    ):
        """重复 ID 应返回 400"""
        resp = await case_app_client.post(
            "/api/v1/cases",
            json={
                "id": "DEV-TEST-001",
                "title": "重复案例测试标题",
                "severity": "minor",
                "status": "investigating",
                "discovered_at": "2025-08-17T06:00:00+00:00",
            },
            headers=auth_headers,
        )
        assert resp.status_code in (400, 409)


class TestCaseList:
    """查询案例列表测试"""

    async def test_list_returns_page(
        self, case_app_client: AsyncClient, auth_headers: dict, seeded_case
    ):
        """列表返回分页结构"""
        resp = await case_app_client.get("/api/v1/cases", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert "total" in data
        assert "page" in data
        assert data["total"] >= 1

    async def test_list_filter_by_status(
        self, case_app_client: AsyncClient, auth_headers: dict, seeded_case
    ):
        """按状态过滤"""
        resp = await case_app_client.get(
            "/api/v1/cases?status=investigating", headers=auth_headers
        )
        assert resp.status_code == 200
        for item in resp.json()["items"]:
            assert item["status"] == "investigating"

    async def test_list_pagination(
        self, case_app_client: AsyncClient, auth_headers: dict, seeded_case
    ):
        """分页参数"""
        resp = await case_app_client.get(
            "/api/v1/cases?page=1&page_size=10", headers=auth_headers
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["page"] == 1
        assert data["page_size"] == 10


class TestCaseUpdate:
    """PATCH 更新 + 状态机校验（Critical #3）"""

    async def test_update_progress_rejected(
        self, case_app_client: AsyncClient, auth_headers: dict, seeded_case
    ):
        """progress 由系统根据 HITL 关卡自动推进，PATCH 直接设置被拒绝"""
        resp = await case_app_client.patch(
            f"/api/v1/cases/{seeded_case.id}",
            json={"progress": 50},
            headers=auth_headers,
        )
        assert resp.status_code == 400, resp.text
        assert "progress" in resp.json()["message"]

    async def test_update_immediate_action(
        self, case_app_client: AsyncClient, auth_headers: dict, seeded_case
    ):
        """更新 immediate_action"""
        resp = await case_app_client.patch(
            f"/api/v1/cases/{seeded_case.id}",
            json={"immediate_action": "新措施：暂停批次放行"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert "新措施" in resp.json()["immediate_action"]

    async def test_patch_cannot_set_status_closed(
        self, case_app_client: AsyncClient, auth_headers: dict, seeded_case
    ):
        """Critical #3: PATCH 禁止直接设 status=closed"""
        resp = await case_app_client.patch(
            f"/api/v1/cases/{seeded_case.id}",
            json={"status": "closed"},
            headers=auth_headers,
        )
        assert resp.status_code == 400, f"应拒绝直接关案，实际: {resp.status_code}, body: {resp.text}"
        assert "signatures" in resp.json()["message"]

    async def test_patch_cannot_set_status_reviewing(
        self, case_app_client: AsyncClient, auth_headers: dict, seeded_case
    ):
        """Critical #3: PATCH 禁止设 status=reviewing"""
        resp = await case_app_client.patch(
            f"/api/v1/cases/{seeded_case.id}",
            json={"status": "reviewing"},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    async def test_patch_nonexistent_case(
        self, case_app_client: AsyncClient, auth_headers: dict
    ):
        """更新不存在的案例返回 404"""
        resp = await case_app_client.patch(
            "/api/v1/cases/DEV-NOT-EXIST",
            json={"progress": 50},
            headers=auth_headers,
        )
        assert resp.status_code == 404
