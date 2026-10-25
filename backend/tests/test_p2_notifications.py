"""P2-⑧ 站内协作通知 + 偏差分级/批次影响测试"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service import capa_routes, notification_routes, routes
from shared.exceptions import NotFoundError
from shared.models import AuditLog, Deviation, Notification, User
from shared.schemas import CapaCreate, DeviationCreate, DeviationUpdate
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
        "id": "DEV-NTF-BASE",
        "title": "通知测试基础案例",
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


async def _notify(db: AsyncSession, user_id: str, type_: str, read: bool = False) -> Notification:
    n = Notification(
        id=f"NTF-{user_id}-{type_}-{int(read)}",
        user_id=user_id,
        case_id=None,
        type=type_,
        payload={"title": f"测试通知 {type_}"},
        read_at=datetime.now(UTC) if read else None,
    )
    db.add(n)
    await db.commit()
    return n


def _create_body(**kw: object) -> DeviationCreate:
    defaults: dict[str, object] = {
        "id": "DEV-NTF-1",
        "title": "通知发射测试案例标题",
        "severity": "minor",
        "complexity": 1,
        "discovered_at": datetime.now(UTC),
    }
    defaults.update(kw)
    return DeviationCreate(**defaults)  # type: ignore[arg-type]


async def _notifications_for(db: AsyncSession, user_id: str, type_: str | None = None) -> list[Notification]:
    stmt = select(Notification).where(Notification.user_id == user_id)
    if type_:
        stmt = stmt.where(Notification.type == type_)
    return list((await db.execute(stmt)).scalars().all())


# ============================================================
#  通知端点
# ============================================================
class TestNotificationEndpoints:
    async def test_list_own_only_and_unread_count(self, test_db: AsyncSession, seed_db):
        lc, jw = await _user(test_db, "U-LC"), await _user(test_db, "U-JW")
        await _notify(test_db, "U-JW", "GATE_READY")
        await _notify(test_db, "U-JW", "CASE_CLOSED", read=True)
        await _notify(test_db, "U-LC", "CAPA_CREATED")

        mine = await notification_routes.list_notifications(
            request=None,
            page=1, page_size=20, db=test_db, auth=_ctx(jw)  # type: ignore[arg-type]
        )
        assert len(mine.items) == 2
        assert mine.unread == 1

        others = await notification_routes.list_notifications(
            request=None,
            page=1, page_size=20, db=test_db, auth=_ctx(lc)  # type: ignore[arg-type]
        )
        assert len(others.items) == 1
        assert others.unread == 1

    async def test_mark_read_and_foreign_blocked(self, test_db: AsyncSession, seed_db):
        lc, jw = await _user(test_db, "U-LC"), await _user(test_db, "U-JW")
        n = await _notify(test_db, "U-JW", "GATE_READY")

        # 他人通知不可见不可改
        with pytest.raises(NotFoundError):
            await notification_routes.mark_notification_read(
                request=None,
                notification_id=n.id, db=test_db, auth=_ctx(lc)  # type: ignore[arg-type]
            )

        out = await notification_routes.mark_notification_read(
            request=None,
            notification_id=n.id, db=test_db, auth=_ctx(jw)  # type: ignore[arg-type]
        )
        assert out.read_at is not None
        mine = await notification_routes.list_notifications(
            request=None,
            page=1, page_size=20, db=test_db, auth=_ctx(jw)  # type: ignore[arg-type]
        )
        assert mine.unread == 0

    async def test_read_all_idempotent(self, test_db: AsyncSession, seed_db):
        jw = await _user(test_db, "U-JW")
        await _notify(test_db, "U-JW", "GATE_READY")
        await _notify(test_db, "U-JW", "CASE_CLOSED")

        first = await notification_routes.mark_all_notifications_read(
            request=None,
            db=test_db, auth=_ctx(jw)  # type: ignore[arg-type]
        )
        assert first["updated"] == 2
        second = await notification_routes.mark_all_notifications_read(
            request=None,
            db=test_db, auth=_ctx(jw)  # type: ignore[arg-type]
        )
        assert second["updated"] == 0


# ============================================================
#  发射点
# ============================================================
class TestEmissions:
    async def test_create_case_assignment_notifies(self, test_db: AsyncSession, seed_db):
        lc = await _user(test_db, "U-LC")
        await routes.create_case(
            body=_create_body(qa_reviewer_id="U-JW"),
            request=None, db=test_db, auth=_ctx(lc),  # type: ignore[arg-type]
        )
        notes = await _notifications_for(test_db, "U-JW", "CASE_ASSIGNED")
        assert len(notes) == 1
        assert notes[0].case_id == "DEV-NTF-1"

    async def test_create_case_self_assign_no_notification(self, test_db: AsyncSession, seed_db):
        jw = await _user(test_db, "U-JW")
        await routes.create_case(
            body=_create_body(qa_reviewer_id="U-JW"),
            request=None, db=test_db, auth=_ctx(jw),  # type: ignore[arg-type]
        )
        assert (await _notifications_for(test_db, "U-JW", "CASE_ASSIGNED")) == []

    async def test_update_case_reassign_notifies_once(self, test_db: AsyncSession, seed_db):
        admin = await _user(test_db, "U-ADMIN")
        await _case(test_db, id="DEV-NTF-2", qa_reviewer_id=None)

        await routes.update_case(
            case_id="DEV-NTF-2",
            body=DeviationUpdate(qa_reviewer_id="U-JW"),
            request=None, db=test_db, auth=_ctx(admin),  # type: ignore[arg-type]
        )
        assert len(await _notifications_for(test_db, "U-JW", "CASE_ASSIGNED")) == 1

        # 重复指派同一人不重复通知
        await routes.update_case(
            case_id="DEV-NTF-2",
            body=DeviationUpdate(qa_reviewer_id="U-JW"),
            request=None, db=test_db, auth=_ctx(admin),  # type: ignore[arg-type]
        )
        assert len(await _notifications_for(test_db, "U-JW", "CASE_ASSIGNED")) == 1

    async def test_sign_approve_notifies_participants(self, test_db: AsyncSession, seed_db):
        from unittest.mock import MagicMock, patch

        from case_service.routes import sign_case
        from shared.auth_middleware import AuthContext
        from shared.models import SignatureRecord
        from shared.schemas import SignatureRequest

        qa = seed_db[1]  # approve 人；investigator U-LC 在 _case 默认值里已引用
        c = await _case(test_db, id="DEV-NTF-3", status="reviewing")
        _ = c
        test_db.add(SignatureRecord(
            id="SIG-DEV-NTF-3-SUBMIT-U-LC", case_id="DEV-NTF-3",
            signer_id="U-LC", signer_role="investigator",
            signature_meaning="提交签名", signed_at=datetime.now(UTC),
        ))
        await test_db.commit()

        with patch("case_service.routes.verify_password", return_value=True):
            await sign_case(
                request=MagicMock(), case_id="DEV-NTF-3",
                body=SignatureRequest(signature_meaning="approve", password="rca-demo-2025"),
                db=test_db, auth=AuthContext(
                    user=qa, session_id="test-session", ip_address="127.0.0.1",
                    permissions={"sign:approve"}, db=None,  # type: ignore[arg-type]
                ),
            )
        closed = await _notifications_for(test_db, "U-LC", "CASE_CLOSED")
        assert len(closed) == 1 and closed[0].case_id == "DEV-NTF-3"

    async def test_capa_create_notifies_investigator_only(self, test_db: AsyncSession, seed_db):
        lc, jw = await _user(test_db, "U-LC"), await _user(test_db, "U-JW")
        await _case(test_db, id="DEV-NTF-4")

        await capa_routes.create_capa(
            request=None,
            case_id="DEV-NTF-4",
            body=CapaCreate(title="更换过滤器", severity="major", responsibility="工程部"),
            db=test_db, auth=_ctx(jw),  # type: ignore[arg-type]
        )
        assert len(await _notifications_for(test_db, "U-LC", "CAPA_CREATED")) == 1

        # 调查员自己创建 CAPA 不自我通知
        await capa_routes.create_capa(
            request=None,
            case_id="DEV-NTF-4",
            body=CapaCreate(title="再次整改", severity="minor", responsibility="QA"),
            db=test_db, auth=_ctx(lc),  # type: ignore[arg-type]
        )
        assert len(await _notifications_for(test_db, "U-LC", "CAPA_CREATED")) == 1


# ============================================================
#  偏差分级 / 批次影响
# ============================================================
class TestDeviationTypeAndBatches:
    async def test_create_with_type_and_batches(self, test_db: AsyncSession, seed_db):
        lc = await _user(test_db, "U-LC")
        out = await routes.create_case(
            body=_create_body(
                deviation_type="lab_oos",
                affected_batches=["B240501", "B240502"],
            ),
            request=None, db=test_db, auth=_ctx(lc),  # type: ignore[arg-type]
        )
        assert out.deviation_type == "lab_oos"
        assert out.affected_batches == ["B240501", "B240502"]

    async def test_create_defaults(self, test_db: AsyncSession, seed_db):
        lc = await _user(test_db, "U-LC")
        out = await routes.create_case(
            body=_create_body(),
            request=None, db=test_db, auth=_ctx(lc),  # type: ignore[arg-type]
        )
        assert out.deviation_type == "process_deviation"
        assert out.affected_batches == []

    async def test_update_type_and_batches_audited(self, test_db: AsyncSession, seed_db):
        admin = await _user(test_db, "U-ADMIN")
        await _case(test_db, id="DEV-NTF-5")

        out = await routes.update_case(
            case_id="DEV-NTF-5",
            body=DeviationUpdate(
                deviation_type="lab_oot",
                affected_batches=["B240599"],
            ),
            request=None, db=test_db, auth=_ctx(admin),  # type: ignore[arg-type]
        )
        assert out.deviation_type == "lab_oot"
        assert out.affected_batches == ["B240599"]

        row = (
            await test_db.execute(
                select(AuditLog)
                .where(AuditLog.action == "UPDATE_DEVIATION", AuditLog.target == "DEV-NTF-5")
                .order_by(AuditLog.seq.desc())
            )
        ).scalars().first()
        assert row is not None
        detail = row.detail or ""
        assert "deviation_type" in detail and "affected_batches" in detail
