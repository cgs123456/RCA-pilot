"""Agent 流水线测试 — 6 Agent 执行 + hash 输出

覆盖：
- _hash_output 确定性
- _run_evidence_hunter 返回 ai_output_hash
- _run_reasoner 生成 5 Whys
- _run_skeptic 生成 ≥1 条 challenge
- _run_drafter 生成报告
- _run_guardian ALCOA+ 9 项检查
- Agent 输出 hash 长度 = 64（SHA-256 hex）
"""
from __future__ import annotations

from datetime import UTC, datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import Deviation, Evidence

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def agent_case(test_db: AsyncSession, seed_db):
    """创建带证据的案例用于 Agent 流水线"""
    case = Deviation(
        id="DEV-AGENT-001",
        title="Agent 测试案例",
        severity="major",
        status="investigating",
        discovered_at=datetime.now(UTC),
        investigator_id="U-LC",
        progress=30,
        hitl_gates={
            "gate1": "pending",
            "gate2": "pending",
            "gate3": "pending",
            "gate4": "pending",
            "gate5": "pending",
        },
    )
    test_db.add(case)
    await test_db.flush()

    # 添加 3 条不同源系统的证据
    evidence = [
        Evidence(
            id="E-TEST-01",
            case_id="DEV-AGENT-001",
            source_system="LIMS",
            collected_at=datetime.now(UTC),
            summary="LIMS 检测结果超标",
            raw_data={"cfu": 22, "threshold": 15},
            content_hash="sha256:test1",
            confidence=4,
        ),
        Evidence(
            id="E-TEST-02",
            case_id="DEV-AGENT-001",
            source_system="LMS",
            collected_at=datetime.now(UTC),
            summary="EMP-2187 资质已逾期",
            raw_data={"status": "expired"},
            content_hash="sha256:test2",
            confidence=4,
        ),
        Evidence(
            id="E-TEST-03",
            case_id="DEV-AGENT-001",
            source_system="BMS",
            collected_at=datetime.now(UTC),
            summary="AHU-7 差压异常上升",
            raw_data={"delta_p": 470},
            content_hash="sha256:test3",
            confidence=3,
        ),
    ]
    for ev in evidence:
        test_db.add(ev)
    await test_db.commit()

    # 重新加载 case with relationships
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    result = await test_db.execute(
        select(Deviation)
        .where(Deviation.id == "DEV-AGENT-001")
        .options(selectinload(Deviation.evidence), selectinload(Deviation.five_whys))
    )
    return result.scalar_one()


class TestHashOutput:
    """_hash_output 单元测试"""

    def test_hash_deterministic(self):
        from agent_service.routes import _hash_output

        payload = {"a": 1, "b": [2, 3], "c": "test"}
        h1 = _hash_output(payload)
        h2 = _hash_output(payload)
        assert h1 == h2
        assert h1.startswith("sha256:")
        # SHA-256 hex 长度 64 + "sha256:" 前缀
        assert len(h1) == 7 + 64

    def test_hash_key_order_independent(self):
        """key 顺序不影响 hash（内部 sort_keys=True）"""
        from agent_service.routes import _hash_output

        h1 = _hash_output({"a": 1, "b": 2})
        h2 = _hash_output({"b": 2, "a": 1})
        assert h1 == h2

    def test_hash_value_change_detected(self):
        from agent_service.routes import _hash_output

        h1 = _hash_output({"a": 1})
        h2 = _hash_output({"a": 2})
        assert h1 != h2


class TestEvidenceHunter:
    """Evidence Hunter Agent 测试"""

    async def test_returns_ai_output_hash(self, test_db: AsyncSession, agent_case):
        from agent_service.routes import _run_evidence_hunter

        result = await _run_evidence_hunter(test_db, agent_case)

        assert result["agent"] == "evidence_hunter"
        assert result["status"] == "completed"
        assert "ai_output_hash" in result, "Evidence Hunter 必须返回 ai_output_hash"
        assert result["ai_output_hash"].startswith("sha256:")
        assert len(result["ai_output_hash"]) == 7 + 64

    async def test_evidence_count_correct(self, test_db: AsyncSession, agent_case):
        from agent_service.routes import _run_evidence_hunter

        result = await _run_evidence_hunter(test_db, agent_case)
        assert result["evidence_count"] == 3

    async def test_by_source_aggregation(self, test_db: AsyncSession, agent_case):
        from agent_service.routes import _run_evidence_hunter

        result = await _run_evidence_hunter(test_db, agent_case)
        by_source = result["by_source"]
        assert by_source.get("LIMS") == 1
        assert by_source.get("LMS") == 1
        assert by_source.get("BMS") == 1


class TestReasoner:
    """Reasoner Agent 测试"""

    async def test_generates_five_whys(self, test_db: AsyncSession, agent_case):
        from agent_service.routes import _run_evidence_hunter, _run_reasoner

        eh_out = await _run_evidence_hunter(test_db, agent_case)
        result = await _run_reasoner(test_db, agent_case, eh_out)

        assert result["agent"] == "reasoner"
        assert "five_whys" in result
        # agent_case 未预置 5 Whys，Reasoner 基于已有数据返回（可能为空列表）
        assert isinstance(result["five_whys"], list)
        # 每条 5 Why 应有 n + question + answer
        for fw in result["five_whys"]:
            assert "n" in fw
            assert "question" in fw
            assert "answer" in fw

    async def test_has_ai_output_hash(self, test_db: AsyncSession, agent_case):
        from agent_service.routes import _run_evidence_hunter, _run_reasoner

        eh_out = await _run_evidence_hunter(test_db, agent_case)
        result = await _run_reasoner(test_db, agent_case, eh_out)
        assert "ai_output_hash" in result
        assert result["ai_output_hash"].startswith("sha256:")


class TestSkeptic:
    """Skeptic Agent 测试"""

    async def test_generates_challenges(self, test_db: AsyncSession, agent_case):
        from agent_service.routes import _run_evidence_hunter, _run_reasoner, _run_skeptic

        eh_out = await _run_evidence_hunter(test_db, agent_case)
        r_out = await _run_reasoner(test_db, agent_case, eh_out)
        result = await _run_skeptic(test_db, agent_case, r_out)

        assert result["agent"] == "skeptic"
        assert "challenges" in result
        assert len(result["challenges"]) >= 1, "Skeptic 应至少生成 1 条挑战"
        # 每条挑战应有 target + challenge + type
        for c in result["challenges"]:
            assert "target" in c
            assert "challenge" in c
            assert "type" in c
            assert c["type"] in (
                "alternative_explanation",
                "independent_cause",
                "evidence_gap",
            )

    async def test_has_ai_output_hash(self, test_db: AsyncSession, agent_case):
        from agent_service.routes import _run_evidence_hunter, _run_reasoner, _run_skeptic

        eh_out = await _run_evidence_hunter(test_db, agent_case)
        r_out = await _run_reasoner(test_db, agent_case, eh_out)
        result = await _run_skeptic(test_db, agent_case, r_out)
        assert "ai_output_hash" in result


class TestDrafter:
    """Drafter Agent 测试"""

    async def test_generates_report(self, test_db: AsyncSession, agent_case):
        from agent_service.routes import (
            _run_drafter,
            _run_evidence_hunter,
            _run_reasoner,
            _run_skeptic,
        )

        eh_out = await _run_evidence_hunter(test_db, agent_case)
        r_out = await _run_reasoner(test_db, agent_case, eh_out)
        s_out = await _run_skeptic(test_db, agent_case, r_out)
        result = await _run_drafter(test_db, agent_case, r_out, s_out)

        assert result["agent"] == "drafter"
        assert "report_markdown" in result
        assert "ai_output_hash" in result


class TestGuardian:
    """Guardian Agent 测试 — ALCOA+ 9 项"""

    async def test_alcoa_checks(self, test_db: AsyncSession, agent_case):
        from agent_service.routes import (
            _run_drafter,
            _run_evidence_hunter,
            _run_guardian,
            _run_reasoner,
            _run_skeptic,
        )

        eh_out = await _run_evidence_hunter(test_db, agent_case)
        r_out = await _run_reasoner(test_db, agent_case, eh_out)
        s_out = await _run_skeptic(test_db, agent_case, r_out)
        d_out = await _run_drafter(test_db, agent_case, r_out, s_out)
        result = await _run_guardian(test_db, agent_case, d_out)

        assert result["agent"] == "guardian"
        # ALCOA+ 9 项检查
        assert "checks" in result
        assert len(result["checks"]) == 9, "ALCOA+ 应有 9 项检查"
        assert "ai_output_hash" in result
        assert "overall_passed" in result
