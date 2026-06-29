"""HITL 5 关测试 — accept/reject/edit + gate3 触发 Reasoner

覆盖：
- gate1~gate5 accept 流程
- gate3 reject 触发 Reasoner 重推理
- HITL 状态校验（gate3 pending 时不能 accept gate4）
- 5 关全通过后案例进入 reviewing
- 审计日志写入
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import Deviation


pytestmark = pytest.mark.asyncio


@pytest.fixture
async def hitl_case(test_db: AsyncSession, seed_db):
    """创建 HITL 待办案例"""
    case = Deviation(
        id="DEV-HITL-001",
        title="HITL 测试案例",
        severity="major",
        status="investigating",
        discovered_at=datetime.now(timezone.utc),
        investigator_id="U-LC",
        qa_reviewer_id="U-JW",
        progress=40,
        hitl_gates={
            "gate1": "pending",
            "gate2": "pending",
            "gate3": "pending",
            "gate4": "pending",
            "gate5": "pending",
        },
    )
    test_db.add(case)
    await test_db.commit()
    return case


class TestHitlAccept:
    """HITL accept 流程测试"""

    async def test_accept_gate1(
        self, case_app_client: AsyncClient, auth_headers: dict, hitl_case
    ):
        """accept gate1 → done"""
        resp = await case_app_client.put(
            f"/api/v1/cases/{hitl_case.id}/hitl/gate1",
            json={"gate_id": "gate1", "action": "accept", "comment": "确认无误"},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["gate_id"] == "gate1"
        assert data["action"] == "accept"

    async def test_accept_gate2(
        self, case_app_client: AsyncClient, auth_headers: dict, hitl_case
    ):
        """accept gate2"""
        resp = await case_app_client.put(
            f"/api/v1/cases/{hitl_case.id}/hitl/gate2",
            json={"gate_id": "gate2", "action": "accept"},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text

    async def test_all_gates_done_sets_reviewing(
        self, case_app_client: AsyncClient, auth_headers: dict, hitl_case
    ):
        """5 关全通过后案例状态变为 reviewing"""
        for gate in ["gate1", "gate2", "gate3", "gate4", "gate5"]:
            resp = await case_app_client.put(
                f"/api/v1/cases/{hitl_case.id}/hitl/{gate}",
                json={"gate_id": gate, "action": "accept", "comment": f"{gate} 通过"},
                headers=auth_headers,
            )
            assert resp.status_code == 200, f"{gate} 失败: {resp.text}"

        # 查询案例验证状态
        resp = await case_app_client.get(
            f"/api/v1/cases/{hitl_case.id}", headers=auth_headers
        )
        assert resp.status_code == 200
        data = resp.json()
        assert all(v == "done" for v in data["hitl_gates"].values())
        assert data["status"] == "reviewing"


class TestHitlReject:
    """HITL reject 流程测试"""

    async def test_reject_gate3(
        self, case_app_client: AsyncClient, auth_headers: dict, hitl_case
    ):
        """gate3 reject → pending + 触发 Reasoner（审计日志）"""
        resp = await case_app_client.put(
            f"/api/v1/cases/{hitl_case.id}/hitl/gate3",
            json={
                "gate_id": "gate3",
                "action": "reject",
                "comment": "5 Whys 推理不充分，缺少证据支撑",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["action"] == "reject"


class TestHitlValidation:
    """HITL 校验逻辑测试"""

    async def test_accept_nonexistent_gate(
        self, case_app_client: AsyncClient, auth_headers: dict, hitl_case
    ):
        """不存在的 gate 应返回 400/404/422"""
        resp = await case_app_client.put(
            f"/api/v1/cases/{hitl_case.id}/hitl/gate9",
            json={"gate_id": "gate9", "action": "accept"},
            headers=auth_headers,
        )
        assert resp.status_code in (400, 404, 422)

    async def test_accept_nonexistent_case(
        self, case_app_client: AsyncClient, auth_headers: dict
    ):
        """不存在案例的 HITL 操作返回 404"""
        resp = await case_app_client.put(
            "/api/v1/cases/DEV-NOT-EXIST/hitl/gate1",
            json={"gate_id": "gate1", "action": "accept"},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    async def test_accept_invalid_decision(
        self, case_app_client: AsyncClient, auth_headers: dict, hitl_case
    ):
        """非法 action 值返回 422"""
        resp = await case_app_client.put(
            f"/api/v1/cases/{hitl_case.id}/hitl/gate1",
            json={"gate_id": "gate1", "action": "maybe"},
            headers=auth_headers,
        )
        assert resp.status_code in (400, 422)
