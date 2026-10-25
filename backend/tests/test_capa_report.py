"""CAPA 生命周期 + ICH Q9 报告版本化 + gate4 自动快照测试（P2-①）"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service import capa_routes, report_routes, routes
from shared.exceptions import ConflictError, NotFoundError, ValidationError
from shared.models import AgentRun, AuditLog, Capa, Deviation, Report, User
from shared.schemas import CapaCreate, CapaUpdate, HitlGateRequest, ReportCreate
from shared.security import ROLE_PERMISSIONS

pytestmark = pytest.mark.asyncio


def _ctx(user: User, db: AsyncSession) -> object:
    return type("Ctx", (), {
        "user": user, "user_id": user.id, "email": user.email, "role": user.role,
        "permissions": ROLE_PERMISSIONS.get(user.role, set()),
        "ip_address": "127.0.0.1", "session_id": "test-session",
    })()


async def _user(db: AsyncSession, uid: str) -> User:
    return (await db.execute(select(User).where(User.id == uid))).scalar_one()


async def _case(db: AsyncSession, **kw) -> Deviation:
    defaults = {
        "id": "DEV-CAPA-1",
        "title": "CAPA 测试案例",
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


async def _audit(db: AsyncSession, action: str) -> AuditLog | None:
    return (
        await db.execute(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.seq.desc()))
    ).scalars().first()


# ============================================================
#  CAPA
# ============================================================
class TestCapa:
    async def test_create_and_list(self, test_db: AsyncSession, seed_db):
        c = await _case(test_db)
        lc = await _user(test_db, "U-LC")
        capa = await capa_routes.create_capa(
            request=None,
            case_id=c.id,
            body=CapaCreate(title="更换 HVAC 高效过滤器", severity="major", responsibility="工程部"),
            db=test_db, auth=_ctx(lc, test_db),  # type: ignore[arg-type]
        )
        assert capa.id.startswith("CAPA-")
        assert capa.status == "pending"
        assert (await _audit(test_db, "CAPA_CREATED")) is not None
        listed = await capa_routes.list_capas(
            case_id=c.id, db=test_db, auth=_ctx(lc, test_db)  # type: ignore[arg-type]
        )
        assert [x.id for x in listed] == [capa.id]

    async def test_create_on_closed_case_blocked(self, test_db: AsyncSession, seed_db):
        c = await _case(test_db, status="closed")
        lc = await _user(test_db, "U-LC")
        with pytest.raises(ValidationError, match="重新开案"):
            await capa_routes.create_capa(
                request=None,
                case_id=c.id,
                body=CapaCreate(title="迟到措施", responsibility="QA"),
                db=test_db, auth=_ctx(lc, test_db),  # type: ignore[arg-type]
            )

    async def test_lifecycle_happy_path(self, test_db: AsyncSession, seed_db):
        c = await _case(test_db)
        lc = await _user(test_db, "U-LC")
        capa = await capa_routes.create_capa(
            request=None,
            case_id=c.id, body=CapaCreate(title="整改", responsibility="生产部"),
            db=test_db, auth=_ctx(lc, test_db),  # type: ignore[arg-type]
        )
        for status in ("under_review", "implemented"):
            capa = await capa_routes.update_capa(
                request=None,
                capa_id=capa.id, body=CapaUpdate(status=status),  # type: ignore[arg-type]
                db=test_db, auth=_ctx(lc, test_db),  # type: ignore[arg-type]
            )
        # verified 必须带有效性评分
        with pytest.raises(ValidationError, match="effectiveness_rate"):
            await capa_routes.update_capa(
                request=None,
                capa_id=capa.id, body=CapaUpdate(status="verified"),
                db=test_db, auth=_ctx(lc, test_db),  # type: ignore[arg-type]
            )
        capa = await capa_routes.update_capa(
            request=None,
            capa_id=capa.id, body=CapaUpdate(status="verified", effectiveness_rate=92),
            db=test_db, auth=_ctx(lc, test_db),  # type: ignore[arg-type]
        )
        assert capa.status == "verified"
        assert capa.verified_by == "U-LC"
        assert capa.verified_at is not None
        assert capa.effectiveness_rate == 92
        capa = await capa_routes.update_capa(
            request=None,
            capa_id=capa.id, body=CapaUpdate(status="closed"),
            db=test_db, auth=_ctx(lc, test_db),  # type: ignore[arg-type]
        )
        assert capa.status == "closed"
        assert (await _audit(test_db, "CAPA_UPDATED")) is not None

    async def test_invalid_transition_rejected(self, test_db: AsyncSession, seed_db):
        c = await _case(test_db)
        lc = await _user(test_db, "U-LC")
        capa = await capa_routes.create_capa(
            request=None,
            case_id=c.id, body=CapaCreate(title="跳级", responsibility="QA"),
            db=test_db, auth=_ctx(lc, test_db),  # type: ignore[arg-type]
        )
        with pytest.raises(ConflictError, match="不允许"):
            await capa_routes.update_capa(
                request=None,
                capa_id=capa.id, body=CapaUpdate(status="closed"),
                db=test_db, auth=_ctx(lc, test_db),  # type: ignore[arg-type]
            )

    async def test_capa_not_found(self, test_db: AsyncSession, seed_db):
        lc = await _user(test_db, "U-LC")
        with pytest.raises(NotFoundError):
            await capa_routes.update_capa(
                request=None,
                capa_id="CAPA-NOPE", body=CapaUpdate(status="under_review"),
                db=test_db, auth=_ctx(lc, test_db),  # type: ignore[arg-type]
            )


# ============================================================
#  报告版本
# ============================================================
class TestReport:
    async def test_versions_auto_increment(self, test_db: AsyncSession, seed_db):
        c = await _case(test_db)
        lc = await _user(test_db, "U-LC")
        v1 = await report_routes.create_report_version(
            request=None,
            case_id=c.id, body=ReportCreate(title="报告", content="# v1", change_reason="初稿"),
            db=test_db, auth=_ctx(lc, test_db),  # type: ignore[arg-type]
        )
        assert v1.version == 1
        v2 = await report_routes.create_report_version(
            request=None,
            case_id=c.id, body=ReportCreate(title="报告", content="# v2", change_reason="采纳 skeptic 意见"),
            db=test_db, auth=_ctx(lc, test_db),  # type: ignore[arg-type]
        )
        assert v2.version == 2
        listed = await report_routes.list_reports(
            case_id=c.id, db=test_db, auth=_ctx(lc, test_db)  # type: ignore[arg-type]
        )
        assert [r.version for r in listed] == [2, 1]

    async def test_get_version_and_404(self, test_db: AsyncSession, seed_db):
        c = await _case(test_db)
        lc = await _user(test_db, "U-LC")
        await report_routes.create_report_version(
            request=None,
            case_id=c.id, body=ReportCreate(title="报告", content="# v1"),
            db=test_db, auth=_ctx(lc, test_db),  # type: ignore[arg-type]
        )
        got = await report_routes.get_report(
            case_id=c.id, version=1, db=test_db, auth=_ctx(lc, test_db)  # type: ignore[arg-type]
        )
        assert got.content == "# v1"
        with pytest.raises(NotFoundError):
            await report_routes.get_report(
                case_id=c.id, version=9, db=test_db, auth=_ctx(lc, test_db)  # type: ignore[arg-type]
            )

    async def test_create_on_closed_case_blocked(self, test_db: AsyncSession, seed_db):
        c = await _case(test_db, status="closed")
        lc = await _user(test_db, "U-LC")
        with pytest.raises(ValidationError, match="重新开案"):
            await report_routes.create_report_version(
                request=None,
                case_id=c.id, body=ReportCreate(title="迟到报告", content="x"),
                db=test_db, auth=_ctx(lc, test_db),  # type: ignore[arg-type]
            )

    async def test_approve_four_eyes(self, test_db: AsyncSession, seed_db):
        c = await _case(test_db)
        lc = await _user(test_db, "U-LC")
        await report_routes.create_report_version(
            request=None,
            case_id=c.id, body=ReportCreate(title="报告", content="# v1"),
            db=test_db, auth=_ctx(lc, test_db),  # type: ignore[arg-type]
        )
        # 作者本人（investigator 无 sign:approve；admin 亦不能批准自己的版本）
        with pytest.raises(ValidationError, match="四眼"):
            await report_routes.approve_report(
                request=None,
                case_id=c.id, version=1, db=test_db,
                auth=_ctx(lc, test_db),  # type: ignore[arg-type]
            )
        jw = await _user(test_db, "U-JW")
        approved = await report_routes.approve_report(
            request=None,
            case_id=c.id, version=1, db=test_db, auth=_ctx(jw, test_db),  # type: ignore[arg-type]
        )
        assert approved.status == "approved"
        assert approved.approved_by == "U-JW"
        with pytest.raises(ConflictError, match="已批准"):
            await report_routes.approve_report(
                request=None,
                case_id=c.id, version=1, db=test_db, auth=_ctx(jw, test_db),  # type: ignore[arg-type]
            )
        assert (await _audit(test_db, "REPORT_APPROVED")) is not None


# ============================================================
#  gate4 通过 → Drafter 报告自动快照 v1
# ============================================================
class TestGate4Snapshot:
    async def _completed_run(self, db: AsyncSession, case_id: str, with_report: bool) -> None:
        states = {"drafter": {"report_markdown": "# 自动快照报告", "report_format": "ICH-Q9-template-v1"}} if with_report else {"drafter": {}}
        db.add(AgentRun(
            id=f"RUN-{case_id}", case_id=case_id, status="completed",
            triggered_by="U-LC", agent_states=states,
            started_at=datetime.now(UTC), completed_at=datetime.now(UTC),
        ))
        await db.commit()

    async def test_gate4_accept_snapshots_report_v1(self, test_db: AsyncSession, seed_db):
        c = await _case(test_db, hitl_gates={"gate1": "done", "gate2": "done", "gate3": "done", "gate4": "pending", "gate5": "pending"})
        await self._completed_run(test_db, c.id, with_report=True)
        lc = await _user(test_db, "U-LC")
        resp = await routes.handle_hitl_gate(
            request=None, case_id=c.id, gate_id="gate4",
            body=HitlGateRequest(gate_id="gate4", action="accept"), db=test_db,
            auth=_ctx(lc, test_db),  # type: ignore[arg-type]
        )
        assert resp.all_gates_passed is False
        report = (
            await test_db.execute(select(Report).where(Report.case_id == c.id))
        ).scalar_one()
        assert report.version == 1
        assert report.content == "# 自动快照报告"
        assert report.author_id == "U-LC"
        assert report.status == "draft"
        assert (await _audit(test_db, "REPORT_VERSION_CREATED")) is not None

    async def test_snapshot_idempotent(self, test_db: AsyncSession, seed_db):
        """已有人工版本时 gate4 通过不再重复快照"""
        c = await _case(test_db, hitl_gates={"gate1": "done", "gate2": "done", "gate3": "done", "gate4": "pending", "gate5": "pending"})
        await self._completed_run(test_db, c.id, with_report=True)
        lc = await _user(test_db, "U-LC")
        await report_routes.create_report_version(
            request=None,
            case_id=c.id, body=ReportCreate(title="人工报告", content="# 人工"),
            db=test_db, auth=_ctx(lc, test_db),  # type: ignore[arg-type]
        )
        await routes.handle_hitl_gate(
            request=None, case_id=c.id, gate_id="gate4",
            body=HitlGateRequest(gate_id="gate4", action="accept"), db=test_db,
            auth=_ctx(lc, test_db),  # type: ignore[arg-type]
        )
        reports = (await test_db.execute(select(Report).where(Report.case_id == c.id))).scalars().all()
        assert [r.version for r in reports] == [1]
        assert reports[0].content == "# 人工"

    async def test_no_drafter_output_no_snapshot(self, test_db: AsyncSession, seed_db):
        c = await _case(test_db, hitl_gates={"gate1": "done", "gate2": "done", "gate3": "done", "gate4": "pending", "gate5": "pending"})
        await self._completed_run(test_db, c.id, with_report=False)
        lc = await _user(test_db, "U-LC")
        await routes.handle_hitl_gate(
            request=None, case_id=c.id, gate_id="gate4",
            body=HitlGateRequest(gate_id="gate4", action="accept"), db=test_db,
            auth=_ctx(lc, test_db),  # type: ignore[arg-type]
        )
        assert (await test_db.execute(select(Report).where(Report.case_id == c.id))).scalar_one_or_none() is None
