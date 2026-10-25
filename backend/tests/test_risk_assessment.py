"""ICH Q9 风险矩阵测试（P2-②）：FMEA 初评、严重度自动升级、风险评估更新端点"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service import routes
from shared.exceptions import NotFoundError, ValidationError
from shared.models import AuditLog, Deviation, User
from shared.schemas import DeviationCreate, FmeaScores, RiskAssessmentUpdate
from shared.security import ROLE_PERMISSIONS

pytestmark = pytest.mark.asyncio


def _ctx(user: User) -> object:
    return type("Ctx", (), {
        "user": user, "user_id": user.id, "email": user.email, "role": user.role,
        "permissions": ROLE_PERMISSIONS.get(user.role, set()),
        "ip_address": "127.0.0.1", "session_id": "test-session",
    })()


async def _user(db: AsyncSession, uid: str) -> User:
    return (await db.execute(select(User).where(User.id == uid))).scalar_one()


async def _case(db: AsyncSession, **kw) -> Deviation:
    defaults = {
        "id": "DEV-RISK-BASE",
        "title": "风险评估基础案例",
        "severity": "minor",
        "status": "investigating",
        "discovered_at": datetime.now(UTC),
        "investigator_id": "U-LC",
        "hitl_gates": {g: "pending" for g in ("gate1", "gate2", "gate3", "gate4", "gate5")},
        "progress": 0,
    }
    defaults.update(kw)
    c = Deviation(**defaults)
    db.add(c)
    await db.commit()
    return c


def _create_body(**kw: object) -> DeviationCreate:
    defaults: dict[str, object] = {
        "id": "DEV-RISK-1",
        "title": "ICH Q9 测试案例标题",
        "severity": "minor",
        "complexity": 1,
        "discovered_at": datetime.now(UTC),
    }
    defaults.update(kw)
    return DeviationCreate(**defaults)  # type: ignore[arg-type]


async def _audit_detail(db: AsyncSession, action: str) -> str | None:
    row = (
        await db.execute(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.seq.desc()))
    ).scalars().first()
    return row.detail if row else None


class TestFmeaOnCreate:
    async def test_rpn_computed_and_category_derived(self, test_db: AsyncSession, seed_db):
        """S=8,O=4,D=3 → RPN=96 → medium；分量快照进 meta["fmea"]"""
        lc = await _user(test_db, "U-LC")
        out = await routes.create_case(
            body=_create_body(fmea=FmeaScores(severity_score=8, occurrence=4, detection=3)),
            request=None, db=test_db, auth=_ctx(lc),  # type: ignore[arg-type]
        )
        assert out.risk_rpn == 96
        assert out.risk_category == "medium"
        assert out.meta["fmea"] == {"severity_score": 8, "occurrence": 4, "detection": 3}
        assert "FMEA 初评 RPN=96（medium）" in (await _audit_detail(test_db, "CREATE_DEVIATION") or "")

    async def test_critical_threshold(self, test_db: AsyncSession, seed_db):
        """RPN=200 恰好落在 critical 档"""
        lc = await _user(test_db, "U-LC")
        out = await routes.create_case(
            body=_create_body(fmea=FmeaScores(severity_score=10, occurrence=10, detection=2)),
            request=None, db=test_db, auth=_ctx(lc),  # type: ignore[arg-type]
        )
        assert out.risk_rpn == 200
        assert out.risk_category == "critical"

    async def test_no_fmea_fields_stay_null(self, test_db: AsyncSession, seed_db):
        lc = await _user(test_db, "U-LC")
        out = await routes.create_case(
            body=_create_body(), request=None, db=test_db, auth=_ctx(lc),  # type: ignore[arg-type]
        )
        assert out.risk_rpn is None
        assert out.risk_category is None
        assert "fmea" not in out.meta


class TestSeverityEscalation:
    async def test_major_high_complexity_escalates(self, test_db: AsyncSession, seed_db):
        lc = await _user(test_db, "U-LC")
        out = await routes.create_case(
            body=_create_body(id="DEV-RISK-2", severity="major", complexity=3),
            request=None, db=test_db, auth=_ctx(lc),  # type: ignore[arg-type]
        )
        assert out.severity == "critical"
        esc = out.meta["severity_escalation"]
        assert esc == {
            "original": "major",
            "effective": "critical",
            "rule": "Major + 高复杂度(3) → critical",
        }
        assert "严重度自动升级: major→critical" in (await _audit_detail(test_db, "CREATE_DEVIATION") or "")

    async def test_minor_high_complexity_escalates(self, test_db: AsyncSession, seed_db):
        lc = await _user(test_db, "U-LC")
        out = await routes.create_case(
            body=_create_body(id="DEV-RISK-3", severity="minor", complexity=3),
            request=None, db=test_db, auth=_ctx(lc),  # type: ignore[arg-type]
        )
        assert out.severity == "major"

    async def test_no_escalation_low_complexity(self, test_db: AsyncSession, seed_db):
        lc = await _user(test_db, "U-LC")
        out = await routes.create_case(
            body=_create_body(id="DEV-RISK-4", severity="minor", complexity=1),
            request=None, db=test_db, auth=_ctx(lc),  # type: ignore[arg-type]
        )
        assert out.severity == "minor"
        assert "severity_escalation" not in out.meta


class TestUpdateRiskEndpoint:
    async def test_update_rpn_category_owner(self, test_db: AsyncSession, seed_db):
        c = await _case(test_db, risk_rpn=96, risk_category="medium")
        lc = await _user(test_db, "U-LC")
        out = await routes.update_risk_assessment(
            case_id=c.id,
            body=RiskAssessmentUpdate(
                fmea=FmeaScores(severity_score=9, occurrence=6, detection=5), risk_owner="U-JW",
            ),
            request=None, db=test_db, auth=_ctx(lc),  # type: ignore[arg-type]
        )
        assert out.risk_rpn == 270
        assert out.risk_category == "critical"
        assert out.risk_owner == "U-JW"
        detail = await _audit_detail(test_db, "RISK_ASSESSMENT_UPDATED")
        assert detail is not None and "RPN 96→270" in detail and "medium→critical" in detail
        await test_db.refresh(c)
        assert c.risk_rpn == 270
        assert c.meta["fmea"]["occurrence"] == 6

    async def test_update_on_closed_case_blocked(self, test_db: AsyncSession, seed_db):
        c = await _case(test_db, status="closed")
        lc = await _user(test_db, "U-LC")
        with pytest.raises(ValidationError, match="重新开案"):
            await routes.update_risk_assessment(
                case_id=c.id,
                body=RiskAssessmentUpdate(fmea=FmeaScores(severity_score=1, occurrence=1, detection=1)),
                request=None, db=test_db, auth=_ctx(lc),  # type: ignore[arg-type]
            )

    async def test_update_404(self, test_db: AsyncSession, seed_db):
        lc = await _user(test_db, "U-LC")
        with pytest.raises(NotFoundError):
            await routes.update_risk_assessment(
                case_id="DEV-NOPE",
                body=RiskAssessmentUpdate(fmea=FmeaScores(severity_score=1, occurrence=1, detection=1)),
                request=None, db=test_db, auth=_ctx(lc),  # type: ignore[arg-type]
            )
