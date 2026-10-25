"""case_service/routes.py 覆盖率补充测试
目标：将 case_service/routes.py 覆盖率从 ~47% 提升到 ≥85%。

策略：
- 多数关键端点采用「直接调用」路由函数（把 Query/Depends 默认参数显式传于
  db/auth，Request 传 None），规避 Python 3.12 + coverage sys.settrace 在
  ASGI + greenlet 切换下丢失「await 恢复后首条语句」line 事件的问题，从而显著
  提高命中率。
- 涉及 PG 专属时序函数（func.to_char / interval）的端点，用自定义 SQLite
  to_char 函数或 Mock db 规避（SQLite 不支持原生 to_char / interval）。
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError as SAIntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from shared.auth_middleware import AuthContext
from shared.database import Base
from shared.models import (
    Capa,
    Deviation,
    FiveWhy,
    SignatureRecord,
    SkepticChallenge,
    SourceSystem,
    User,
)
from shared.schemas import (
    DeviationCreate,
    DeviationUpdate,
    FiveWhyUpdate,
    HitlGateRequest,
    SignatureRequest,
    SkepticResolve,
)

pytestmark = pytest.mark.asyncio


# ============================================================
#  工具函数 —— 构造 AuthContext / 直接插入测试数据
# ============================================================
async def _user(db: AsyncSession, uid: str) -> User:
    return (await db.execute(select(User).where(User.id == uid))).scalar_one()


def _ctx(user: User, db: AsyncSession, permissions: set[str] | None = None) -> AuthContext:
    from shared.security import ROLE_PERMISSIONS

    return AuthContext(
        user=user,
        session_id="test-session",
        ip_address="127.0.0.1",
        permissions=permissions or ROLE_PERMISSIONS.get(user.role, set()),
        db=db,
    )


async def _case(db: AsyncSession, **kw) -> Deviation:
    defaults = {
        "id": "DEV-X-001",
        "title": "默认测试案例标题",
        "severity": "minor",
        "status": "pending",
        "discovered_at": datetime.now(UTC),
        "hitl_gates": {g: "pending" for g in ("gate1", "gate2", "gate3", "gate4", "gate5")},
        "progress": 0,
    }
    defaults.update(kw)
    c = Deviation(**defaults)
    db.add(c)
    await db.commit()
    return c


# ============================================================
#  加载 case_service 路由（供直接调用）
# ============================================================
from case_service import routes  # noqa: E402


# ============================================================
#  带 to_char 支持的 SQLite 引擎 —— 用于 create_case 自动 ID 路径
# ============================================================
def _sqlite_to_char(col, fmt):
    if col is None:
        return None
    s = str(col)
    if fmt == "YYYY-MM":
        return s[:7]
    if fmt == "YYYY-MM-DD":
        return s[:10]
    return s


@pytest.fixture
async def tochar_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)

    @event.listens_for(engine.sync_engine, "connect")
    def _reg(dbapi_conn, conn_record):
        dbapi_conn.create_function("to_char", 2, _sqlite_to_char)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def tochar_db(tochar_engine) -> AsyncGenerator[AsyncSession, None]:
    factory = async_sessionmaker(
        tochar_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    async with factory() as session:
        # 播种 U-LC 用户（investigator）
        from shared.security import hash_password

        session.add(
            User(
                id="U-LC",
                email="lisa.chen@biopharma.com",
                name="Lisa Chen",
                role="investigator",
                password_hash=hash_password("rca-demo-2025"),
                is_active=True,
            )
        )
        await session.commit()
        yield session
        await session.rollback()


@pytest.fixture
async def fk_engine():
    """强制外键的 SQLite 引擎 —— 默认 test_db 不开 PRAGMA，无法触发 FK 违反分支"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


# ============================================================
#  list_cases —— 过滤 + 分页
# ============================================================
class TestListCases:
    async def test_filters_and_pagination(
        self, test_db: AsyncSession, seed_db
    ):
        base = datetime.now(UTC)
        await _case(
            test_db, id="DEV-LC-001", title="微生物偏差 alpha",
            severity="major", status="investigating",
            investigator_id="U-LC", discovered_at=base,
        )
        await _case(
            test_db, id="DEV-LC-002", title="颗粒偏差 beta",
            severity="minor", status="pending",
            investigator_id="U-JW", discovered_at=base,
        )
        for i in range(3):
            await _case(
                test_db, id=f"DEV-LC-00{i + 3}", title="堆量案例 gamma",
                severity="critical", status="pending", investigator_id="U-LC",
                discovered_at=base,
            )
        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)

        # 直接调用时必须把所有 Query 默认参数显式传值，否则会绑定 FastAPI Query 对象
        # severity 过滤 (91)
        r = await routes.list_cases(
            request=None, severity="major", status=None, investigator_id=None,
            keyword=None, page=1, page_size=20, db=test_db, _=ctx
        )
        assert r.total == 1 and r.items[0].id == "DEV-LC-001"

        # investigator_id 过滤 (93)
        r = await routes.list_cases(
            request=None, investigator_id="U-LC", status=None, severity=None,
            keyword=None, page=1, page_size=20, db=test_db, _=ctx
        )
        assert r.total == 4

        # keyword 过滤 (95-96)
        r = await routes.list_cases(
            request=None, keyword="DEV-LC-002", status=None, severity=None,
            investigator_id=None, page=1, page_size=20, db=test_db, _=ctx
        )
        assert r.total == 1
        r = await routes.list_cases(
            request=None, keyword="微生物", status=None, severity=None,
            investigator_id=None, page=1, page_size=20, db=test_db, _=ctx
        )
        assert all("微生物" in c.title or "微生物" in c.id for c in r.items)
        assert r.total == 1

        # 分页 offset/limit (115, 120)
        r = await routes.list_cases(
            request=None, status=None, severity=None, investigator_id=None,
            keyword=None, page=1, page_size=2, db=test_db, _=ctx
        )
        assert len(r.items) == 2 and r.page == 1 and r.page_size == 2
        r = await routes.list_cases(
            request=None, status=None, severity=None, investigator_id=None,
            keyword=None, page=3, page_size=2, db=test_db, _=ctx
        )
        assert len(r.items) == 1

    async def test_status_and_count_filters(
        self, test_db: AsyncSession, seed_db
    ):
        await _case(test_db, id="DEV-LC-010", status="pending")
        await _case(test_db, id="DEV-LC-011", status="closed")
        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        r = await routes.list_cases(
            request=None, status="closed", severity=None, investigator_id=None,
            keyword=None, page=1, page_size=20, db=test_db, _=ctx
        )
        assert r.total == 1 and r.items[0].id == "DEV-LC-011"


# ============================================================
#  get_case —— 404 分支
# ============================================================
class TestGetCase:
    async def test_not_found(self, test_db: AsyncSession, seed_db):
        from shared.exceptions import NotFoundError

        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        with pytest.raises(NotFoundError):
            await routes.get_case(
                request=None, case_id="DEV-NOT-EXIST", db=test_db, _=ctx
            )

    async def test_success(self, test_db: AsyncSession, seed_db):
        c = await _case(test_db, id="DEV-GET-001")
        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        r = await routes.get_case(
            request=None, case_id=c.id, db=test_db, _=ctx
        )
        assert r.id == c.id


# ============================================================
#  create_case —— 自动 ID / 冲突重试 / 冲突报 400
# ============================================================
class TestCreateCase:
    async def test_auto_id_and_exhaust_retry(self, tochar_db: AsyncSession):
        from shared.exceptions import ConflictError

        lc = await _user(tochar_db, "U-LC")
        # 先触发属性加载，再脱离 session —— 避免路由内因冲突触发 rollback 使
        # auth.user 属性过期，进而在同步链路里触发 async 惰性加载（MissingGreenlet）
        _ = lc.id, lc.email, lc.role
        tochar_db.expunge(lc)
        ctx = _ctx(lc, tochar_db)
        month = datetime.now(UTC).strftime("%Y-%m")
        # 预置一个非连续 ID（002）—— 让自动生成的 DEV-{month}-002 与已有冲突，
        # 使 count+1 生成的 ID 命中已存在记录 → 触发 IntegrityError → continue → 上限抛错
        await _case(
            tochar_db,
            id=f"DEV-{month}-002", title="占位冲突案例",
            discovered_at=datetime.now(UTC), status="pending",
        )

        with pytest.raises(ConflictError):
            await routes.create_case(
                request=None,
                body=DeviationCreate(
                    title="自动生成 ID 案例",
                    severity="major",
                    discovered_at=datetime.now(UTC),
                ),
                db=tochar_db,
                auth=ctx,
            )

    async def test_auto_id_success(self, tochar_db: AsyncSession):
        lc = await _user(tochar_db, "U-LC")
        ctx = _ctx(lc, tochar_db)
        r = await routes.create_case(
            request=None,
            body=DeviationCreate(
                title="自动成功案例标题", severity="minor",
                discovered_at=datetime.now(UTC),
            ),
            db=tochar_db,
            auth=ctx,
        )
        assert r.id.startswith("DEV-")
        assert r.title == "自动成功案例标题"

    async def test_explicit_id_conflict(
        self, test_db: AsyncSession, seed_db
    ):
        from shared.exceptions import ValidationError

        await _case(test_db, id="DEV-FIXED-1", status="pending")
        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        with pytest.raises(ValidationError):
            await routes.create_case(
                request=None,
                body=DeviationCreate(
                    id="DEV-FIXED-1", title="重复 ID 案例", severity="minor",
                    discovered_at=datetime.now(UTC),
                ),
                db=test_db,
                auth=ctx,
            )

    async def test_explicit_id_success(
        self, test_db: AsyncSession, seed_db
    ):
        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        r = await routes.create_case(
            request=None,
            body=DeviationCreate(
                id="DEV-NEW-1", title="显式 ID 成功标题",
                severity="critical", discovered_at=datetime.now(UTC),
            ),
            db=test_db,
            auth=ctx,
        )
        assert r.id == "DEV-NEW-1"

    async def test_fk_violation_missing_user(self, fk_engine):
        """investigator_id 指向不存在用户 → 立即 422，而非空转重试 5 次后误报 ID 冲突"""
        from shared.exceptions import ValidationError
        from shared.security import hash_password

        factory = async_sessionmaker(
            fk_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )
        async with factory() as db:
            db.add(
                User(
                    id="U-LC",
                    email="lisa.chen@demo.local",
                    name="Lisa Chen",
                    role="investigator",
                    password_hash=hash_password("rca-demo-2025"),
                    is_active=True,
                )
            )
            await db.commit()
            lc = (await db.execute(select(User).where(User.id == "U-LC"))).scalar_one()
            _ = lc.id, lc.email, lc.role  # 预加载属性，防 rollback 后惰性加载 MissingGreenlet
            db.expunge(lc)
            ctx = _ctx(lc, db)
            with pytest.raises(ValidationError, match="关联的用户不存在"):
                await routes.create_case(
                    request=None,
                    body=DeviationCreate(
                        id="DEV-FK-001",
                        title="关联用户不存在案例标题",
                        severity="minor",
                        discovered_at=datetime.now(UTC),
                        investigator_id="U-GHOST",
                    ),
                    db=db,
                    auth=ctx,
                )


# ============================================================
#  update_case —— 状态机 / 清空 / 补丁字段
# ============================================================
class TestUpdateCase:
    async def test_not_found(self, test_db: AsyncSession, seed_db):
        from shared.exceptions import NotFoundError

        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        with pytest.raises(NotFoundError):
            await routes.update_case(
                request=None, case_id="DEV-NOT-EXIST",
                body=DeviationUpdate(title="x"), db=test_db, auth=ctx,
            )

    async def test_closed_rejected(self, test_db: AsyncSession, seed_db):
        from shared.exceptions import ValidationError

        c = await _case(test_db, id="DEV-UP-1", status="closed")
        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        with pytest.raises(ValidationError):
            await routes.update_case(
                request=None, case_id=c.id,
                body=DeviationUpdate(immediate_action="试图改"), db=test_db, auth=ctx,
            )

    async def test_status_closed_rejected(self, test_db: AsyncSession, seed_db):
        from shared.exceptions import ValidationError

        c = await _case(test_db, id="DEV-UP-2", status="pending")
        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        with pytest.raises(ValidationError):
            await routes.update_case(
                request=None, case_id=c.id,
                body=DeviationUpdate(status="closed"), db=test_db, auth=ctx,
            )

    async def test_status_reviewing_rejected(self, test_db: AsyncSession, seed_db):
        from shared.exceptions import ValidationError

        c = await _case(test_db, id="DEV-UP-3", status="pending")
        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        with pytest.raises(ValidationError):
            await routes.update_case(
                request=None, case_id=c.id,
                body=DeviationUpdate(status="reviewing"), db=test_db, auth=ctx,
            )

    async def test_progress_rejected(self, test_db: AsyncSession, seed_db):
        from shared.exceptions import ValidationError

        c = await _case(test_db, id="DEV-UP-4", status="pending")
        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        with pytest.raises(ValidationError):
            await routes.update_case(
                request=None, case_id=c.id,
                body=DeviationUpdate(progress=50), db=test_db, auth=ctx,
            )

    async def test_status_value_not_investigating(self, test_db: AsyncSession, seed_db):
        from shared.exceptions import ValidationError

        c = await _case(test_db, id="DEV-UP-5", status="pending")
        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        # status=pending，非 investigating → 299-302 raise
        with pytest.raises(ValidationError):
            await routes.update_case(
                request=None, case_id=c.id,
                body=DeviationUpdate(status="pending"), db=test_db, auth=ctx,
            )

    async def test_status_transition_from_non_pending(
        self, test_db: AsyncSession, seed_db
    ):
        from shared.exceptions import ValidationError

        c = await _case(test_db, id="DEV-UP-6", status="investigating")
        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        with pytest.raises(ValidationError):
            await routes.update_case(
                request=None, case_id=c.id,
                body=DeviationUpdate(status="investigating"), db=test_db, auth=ctx,
            )

    async def test_patch_field_skip_and_apply(self, test_db: AsyncSession, seed_db):
        c = await _case(
            test_db, id="DEV-UP-7", status="pending",
            description="原始描述", location="Suite 1",
        )
        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        # meta=None（不可清空字段）→ 跳过；description=None（可清空）→ 置空；
        # immediate_action 正常打补丁；status=pending→investigating 白名单放行
        r = await routes.update_case(
            request=None, case_id=c.id,
            body=DeviationUpdate(
                meta=None, description=None,
                immediate_action="暂停批次放行", status="investigating",
            ),
            db=test_db, auth=ctx,
        )
        assert r.status == "investigating"
        assert r.description is None
        assert r.immediate_action == "暂停批次放行"


# ============================================================
#  handle_hitl_gate —— accept/reject/edit 全分支
# ============================================================
class TestHandleHitlGate:
    async def test_path_body_gate_mismatch(
        self, test_db: AsyncSession, seed_db
    ):
        from shared.exceptions import ValidationError

        c = await _case(test_db, id="DEV-HG-0")
        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        with pytest.raises(ValidationError):
            await routes.handle_hitl_gate(
                request=None, case_id=c.id, gate_id="gate1",
                body=HitlGateRequest(gate_id="gate2", action="accept"),
                db=test_db, auth=ctx,
            )

    async def test_invalid_gate(self, test_db: AsyncSession, seed_db):
        from shared.exceptions import ValidationError

        c = await _case(test_db, id="DEV-HG-1")
        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        body = HitlGateRequest.model_construct(gate_id="gate9", action="accept")
        with pytest.raises(ValidationError):
            await routes.handle_hitl_gate(
                request=None, case_id=c.id, gate_id="gate9",
                body=body, db=test_db, auth=ctx,
            )

    async def test_case_not_found(self, test_db: AsyncSession, seed_db):
        from shared.exceptions import NotFoundError

        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        with pytest.raises(NotFoundError):
            await routes.handle_hitl_gate(
                request=None, case_id="DEV-NOT-EXIST", gate_id="gate1",
                body=HitlGateRequest(gate_id="gate1", action="accept"),
                db=test_db, auth=ctx,
            )

    async def test_closed_case(self, test_db: AsyncSession, seed_db):
        from shared.exceptions import ValidationError

        c = await _case(test_db, id="DEV-HG-2", status="closed")
        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        with pytest.raises(ValidationError):
            await routes.handle_hitl_gate(
                request=None, case_id=c.id, gate_id="gate1",
                body=HitlGateRequest(gate_id="gate1", action="accept"),
                db=test_db, auth=ctx,
            )

    async def test_order_enforced(self, test_db: AsyncSession, seed_db):
        from shared.exceptions import ValidationError

        c = await _case(test_db, id="DEV-HG-3")
        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        with pytest.raises(ValidationError):
            await routes.handle_hitl_gate(
                request=None, case_id=c.id, gate_id="gate2",
                body=HitlGateRequest(gate_id="gate2", action="accept"),
                db=test_db, auth=ctx,
            )

    async def test_duplicate_done(self, test_db: AsyncSession, seed_db):
        from shared.exceptions import ValidationError

        gates = {g: "done" if g == "gate1" else "pending" for g in ("gate1", "gate2", "gate3", "gate4", "gate5")}
        c = await _case(test_db, id="DEV-HG-4", hitl_gates=gates)
        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        with pytest.raises(ValidationError):
            await routes.handle_hitl_gate(
                request=None, case_id=c.id, gate_id="gate1",
                body=HitlGateRequest(gate_id="gate1", action="accept"),
                db=test_db, auth=ctx,
            )

    async def test_edit_only_gate2(self, test_db: AsyncSession, seed_db):
        from shared.exceptions import ValidationError

        c = await _case(test_db, id="DEV-HG-5")
        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        with pytest.raises(ValidationError):
            await routes.handle_hitl_gate(
                request=None, case_id=c.id, gate_id="gate1",
                body=HitlGateRequest(
                    gate_id="gate1", action="edit",
                    edit_payload={"five_whys": []},
                ),
                db=test_db, auth=ctx,
            )

    async def test_edit_missing_whys(self, test_db: AsyncSession, seed_db):
        from shared.exceptions import ValidationError

        gates = {g: ("done" if g == "gate1" else "pending")
                 for g in ("gate1", "gate2", "gate3", "gate4", "gate5")}
        c = await _case(test_db, id="DEV-HG-6", hitl_gates=gates)
        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        with pytest.raises(ValidationError):
            await routes.handle_hitl_gate(
                request=None, case_id=c.id, gate_id="gate2",
                body=HitlGateRequest(
                    gate_id="gate2", action="edit",
                    edit_payload={},
                ),
                db=test_db, auth=ctx,
            )

    async def test_edit_invalid_entry(self, test_db: AsyncSession, seed_db):
        from shared.exceptions import ValidationError

        gates = {g: ("done" if g == "gate1" else "pending")
                 for g in ("gate1", "gate2", "gate3", "gate4", "gate5")}
        c = await _case(test_db, id="DEV-HG-7", hitl_gates=gates)
        for n in range(1, 6):
            db_add_fw(test_db, f"FW-HG7-{n}", c.id, n, "旧问", "旧答")
        await test_db.commit()
        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        with pytest.raises(ValidationError):
            await routes.handle_hitl_gate(
                request=None, case_id=c.id, gate_id="gate2",
                body=HitlGateRequest(
                    gate_id="gate2", action="edit",
                    edit_payload={"five_whys": [{"n": 9, "why_text": "不存在"}]},
                ),
                db=test_db, auth=ctx,
            )

    async def test_edit_success_to_reviewing(self, test_db: AsyncSession, seed_db):
        gates = {g: ("done" if g in ("gate1", "gate3", "gate4", "gate5") else "pending")
                 for g in ("gate1", "gate2", "gate3", "gate4", "gate5")}
        c = await _case(test_db, id="DEV-HG-8", hitl_gates=gates, status="pending")
        for n in range(1, 6):
            db_add_fw(test_db, f"FW-HG8-{n}", c.id, n, "旧问", f"旧答{n}")
        await test_db.commit()
        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        r = await routes.handle_hitl_gate(
            request=None, case_id=c.id, gate_id="gate2",
            body=HitlGateRequest(
                gate_id="gate2", action="edit",
                edit_payload={"five_whys": [{"n": 1, "why_text": "新答1"}]},
            ),
            db=test_db, auth=ctx,
        )
        assert r.new_status == "reviewing" and r.all_gates_passed is True

    async def test_invalid_action(self, test_db: AsyncSession, seed_db):
        from shared.exceptions import ValidationError

        c = await _case(test_db, id="DEV-HG-9")
        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        body = HitlGateRequest.model_construct(gate_id="gate1", action="maybe")
        with pytest.raises(ValidationError):
            await routes.handle_hitl_gate(
                request=None, case_id=c.id, gate_id="gate1",
                body=body, db=test_db, auth=ctx,
            )

    async def test_accept_gate1_investigating(self, test_db: AsyncSession, seed_db):
        c = await _case(test_db, id="DEV-HG-10", status="pending")
        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        r = await routes.handle_hitl_gate(
            request=None, case_id=c.id, gate_id="gate1",
            body=HitlGateRequest(gate_id="gate1", action="accept"),
            db=test_db, auth=ctx,
        )
        assert r.new_status == "investigating" and r.triggered_agent is None

    async def test_all_passed_to_reviewing(self, test_db: AsyncSession, seed_db):
        gates = {g: ("done" if g in ("gate1", "gate2", "gate3", "gate4") else "pending")
                 for g in ("gate1", "gate2", "gate3", "gate4", "gate5")}
        c = await _case(test_db, id="DEV-HG-11", hitl_gates=gates, status="pending")
        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        r = await routes.handle_hitl_gate(
            request=None, case_id=c.id, gate_id="gate5",
            body=HitlGateRequest(gate_id="gate5", action="accept"),
            db=test_db, auth=ctx,
        )
        assert r.new_status == "reviewing" and r.all_gates_passed is True

    async def test_gate3_accept_reasoner(self, test_db: AsyncSession, seed_db):
        gates = {g: ("done" if g in ("gate1", "gate2") else "pending")
                 for g in ("gate1", "gate2", "gate3", "gate4", "gate5")}
        c = await _case(test_db, id="DEV-HG-12", hitl_gates=gates, status="pending")
        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        r = await routes.handle_hitl_gate(
            request=None, case_id=c.id, gate_id="gate3",
            body=HitlGateRequest(gate_id="gate3", action="accept"),
            db=test_db, auth=ctx,
        )
        assert r.triggered_agent == "reasoner"

    async def test_gate3_reject_auto_retry(self, test_db: AsyncSession, seed_db):
        gates = {g: ("done" if g in ("gate1", "gate2") else "pending")
                 for g in ("gate1", "gate2", "gate3", "gate4", "gate5")}
        c = await _case(test_db, id="DEV-HG-13", hitl_gates=gates, meta={}, status="investigating")
        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        r = await routes.handle_hitl_gate(
            request=None, case_id=c.id, gate_id="gate3",
            body=HitlGateRequest(gate_id="gate3", action="reject", comment="推理不充分"),
            db=test_db, auth=ctx,
        )
        assert r.triggered_agent == "reasoner"
        # audit 写入了 gate3_retry_count
        (await test_db.execute(select(Deviation).where(Deviation.id == c.id))).scalar_one()

    async def test_gate3_reject_retry_limit(self, test_db: AsyncSession, seed_db):
        gates = {g: ("done" if g in ("gate1", "gate2") else "pending")
                 for g in ("gate1", "gate2", "gate3", "gate4", "gate5")}
        c = await _case(
            test_db, id="DEV-HG-14", hitl_gates=gates,
            meta={"gate3_retry_count": 3}, status="investigating",
        )
        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        r = await routes.handle_hitl_gate(
            request=None, case_id=c.id, gate_id="gate3",
            body=HitlGateRequest(gate_id="gate3", action="reject"),
            db=test_db, auth=ctx,
        )
        assert r.triggered_agent is None


def db_add_fw(db, fw_id, case_id, n, question, answer):
    db.add(
        FiveWhy(
            id=fw_id, case_id=case_id, n=n,
            question=question, answer=answer, status="pending",
        )
    )


# ============================================================
#  _trigger_pipeline_retry —— 派发结果三态（dispatched/skipped/failed）
# ============================================================
class TestTriggerPipelineRetry:
    async def test_exception_returns_failed(self, test_db: AsyncSession, monkeypatch):
        from shared.config import settings

        async def _boom(*args, **kwargs):
            raise OSError("network down")

        monkeypatch.setattr(settings, "service_api_key", "secret-key")
        monkeypatch.setattr("httpx.AsyncClient.post", _boom)
        # B11：网络失败不抛出 —— 返回 failed 由调用方显式审计
        result = await routes._trigger_pipeline_retry(
            case_id="DEV-1", operator="admin@x.com"
        )
        assert result == "failed"

    async def test_no_api_key_returns_skipped(self, test_db: AsyncSession, monkeypatch):
        from shared.config import settings

        monkeypatch.setattr(settings, "service_api_key", "")
        result = await routes._trigger_pipeline_retry(
            case_id="DEV-1", operator="a@b.com"
        )
        assert result == "skipped"

    async def test_http_success_returns_dispatched(self, test_db: AsyncSession, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        from shared.config import settings

        mock_response = MagicMock(status_code=200)
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        monkeypatch.setattr(settings, "service_api_key", "secret-key")
        monkeypatch.setattr("httpx.AsyncClient", MagicMock(return_value=mock_client))

        result = await routes._trigger_pipeline_retry(
            case_id="DEV-1", operator="a@b.com"
        )
        assert result == "dispatched"
        assert mock_client.post.call_args.kwargs["headers"]["X-Service-Key"] == "secret-key"

    async def test_http_5xx_returns_failed(self, test_db: AsyncSession, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        import httpx as _httpx

        from shared.config import settings

        mock_response = MagicMock(status_code=500)
        mock_response.raise_for_status = MagicMock(
            side_effect=_httpx.HTTPStatusError(
                "500 Internal Server Error",
                request=MagicMock(),
                response=MagicMock(status_code=500),
            )
        )
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        monkeypatch.setattr(settings, "service_api_key", "secret-key")
        monkeypatch.setattr("httpx.AsyncClient", MagicMock(return_value=mock_client))

        result = await routes._trigger_pipeline_retry(
            case_id="DEV-1", operator="a@b.com"
        )
        assert result == "failed"


# ============================================================
#  update_five_why —— 404 + 修改分支
# ============================================================
class TestUpdateFiveWhy:
    async def test_not_found(self, test_db: AsyncSession, seed_db):
        from shared.exceptions import NotFoundError

        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        with pytest.raises(NotFoundError):
            await routes.update_five_why(
                request=None, case_id="DEV-X", why_id="FW-NOT-EXIST",
                body=FiveWhyUpdate(answer="新答"), db=test_db, auth=ctx,
            )

    async def test_update_fields(self, test_db: AsyncSession, seed_db):
        from shared.models import Evidence

        c = await _case(test_db, id="DEV-FW-1")
        db_add_fw(test_db, "FW-1", c.id, 1, "为什么?", "因为 A")
        # update_five_why 现校验 evidence_ids 必须属于本案例 —— 预置一条真实证据
        test_db.add(Evidence(
            id="E-1", case_id=c.id, source_system="MES",
            collected_at=datetime.now(UTC), content_hash="sha256:test",
        ))
        await test_db.commit()
        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        r = await routes.update_five_why(
            request=None, case_id=c.id, why_id="FW-1",
            body=FiveWhyUpdate(
                answer="因为 B", question="为什么?②", status="edited",
                evidence_ids=["E-1"],
            ),
            db=test_db, auth=ctx,
        )
        assert any(fw.id == "FW-1" and fw.answer == "因为 B" for fw in r.five_whys)


# ============================================================
#  resolve_skeptic —— 404 / accepted / rejected / all_passed
# ============================================================
class TestResolveSkeptic:
    async def test_challenge_not_found(self, test_db: AsyncSession, seed_db):
        from shared.exceptions import NotFoundError

        c = await _case(test_db, id="DEV-SK-0")
        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        with pytest.raises(NotFoundError):
            await routes.resolve_skeptic(
                request=None, case_id=c.id, challenge_id="SC-NOT-EXIST",
                body=SkepticResolve(status="accepted", resolution="x"),
                db=test_db, auth=ctx,
            )

    async def test_case_not_found(self, test_db: AsyncSession, seed_db):
        from shared.exceptions import NotFoundError

        c = await _case(test_db, id="DEV-SK-1")
        add_sc(test_db, "SC-1", c.id)
        await test_db.commit()
        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        with pytest.raises(NotFoundError):
            await routes.resolve_skeptic(
                request=None, case_id="DEV-NOT-EXIST", challenge_id="SC-1",
                body=SkepticResolve(status="accepted", resolution="x"),
                db=test_db, auth=ctx,
            )

    async def test_challenge_orphan_no_case(self, test_db: AsyncSession, seed_db):
        # 挑战仍存在，但其归属案例已被删除 → 命中 resolve_skeptic 内 case is None 分支。
        # 用原始 DELETE 绕开 ORM 的 delete-orphan 级联（否则候选挑战也会被一并删掉，
        # 提前命中 challenge 未找到分支而不是 case 未找到分支）。
        from sqlalchemy import text

        from shared.exceptions import NotFoundError

        c = await _case(test_db, id="DEV-SK-ORPHAN")
        add_sc(test_db, "SC-ORPHAN", c.id)
        await test_db.commit()
        await test_db.execute(text("DELETE FROM deviations WHERE id='DEV-SK-ORPHAN'"))
        await test_db.commit()
        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        with pytest.raises(NotFoundError):
            await routes.resolve_skeptic(
                request=None, case_id="DEV-SK-ORPHAN", challenge_id="SC-ORPHAN",
                body=SkepticResolve(status="accepted", resolution="x"),
                db=test_db, auth=ctx,
            )

    async def test_accepted(self, test_db: AsyncSession, seed_db):
        c = await _case(test_db, id="DEV-SK-2", status="pending")
        add_sc(test_db, "SC-2", c.id, status="pending")
        await test_db.commit()
        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        r = await routes.resolve_skeptic(
            request=None, case_id=c.id, challenge_id="SC-2",
            body=SkepticResolve(status="accepted", resolution="确认无风险"),
            db=test_db, auth=ctx,
        )
        assert r.action == "accept" and r.case_id == c.id

    async def test_rejected(self, test_db: AsyncSession, seed_db):
        c = await _case(test_db, id="DEV-SK-3", status="pending")
        add_sc(test_db, "SC-3", c.id, status="rejected")
        await test_db.commit()
        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        r = await routes.resolve_skeptic(
            request=None, case_id=c.id, challenge_id="SC-3",
            body=SkepticResolve(status="rejected", resolution="需补充证据"),
            db=test_db, auth=ctx,
        )
        assert r.action == "reject"

    async def test_all_passed_reviewing(self, test_db: AsyncSession, seed_db):
        gates = {g: "done" for g in ("gate1", "gate2", "gate3", "gate4", "gate5")}
        c = await _case(test_db, id="DEV-SK-4", status="pending", hitl_gates=gates)
        add_sc(test_db, "SC-4", c.id)
        await test_db.commit()
        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        r = await routes.resolve_skeptic(
            request=None, case_id=c.id, challenge_id="SC-4",
            body=SkepticResolve(status="accepted", resolution="确认"),
            db=test_db, auth=ctx,
        )
        assert r.new_status == "reviewing" and r.all_gates_passed is True


def add_sc(db, sc_id, case_id, status="pending"):
    db.add(
        SkepticChallenge(
            id=sc_id, case_id=case_id, target="结论", challenge="质疑",
            status=status,
        )
    )


# ============================================================
#  sign_case —— 密码/权限/404/重复/顺序/IntegrityError
# ============================================================
class TestSignCase:
    async def _reviewing_case(self, db: AsyncSession, cid: str) -> Deviation:
        gates = {g: "done" for g in ("gate1", "gate2", "gate3", "gate4", "gate5")}
        return await _case(db, id=cid, status="reviewing", hitl_gates=gates)

    async def test_wrong_password(self, test_db: AsyncSession, seed_db):
        from shared.exceptions import ForbiddenError

        c = await self._reviewing_case(test_db, "DEV-SG-0")
        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        with pytest.raises(ForbiddenError):
            await routes.sign_case(
                request=None, case_id=c.id,
                body=SignatureRequest(signature_meaning="submit", password="wrong-password"),
                db=test_db, auth=ctx,
            )

    async def test_submit_permission_check(self, test_db: AsyncSession, seed_db):
        # investigator 有 sign:submit → 权限校验通过后到达案例查找 → 404
        from shared.exceptions import NotFoundError

        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        with pytest.raises(NotFoundError):
            await routes.sign_case(
                request=None, case_id="DEV-NOT-EXIST",
                body=SignatureRequest(signature_meaning="submit", password="rca-demo-2025"),
                db=test_db, auth=ctx,
            )

    async def test_approve_forbidden_for_investigator(
        self, test_db: AsyncSession, seed_db
    ):
        from shared.exceptions import ForbiddenError

        lc = (await _user(test_db, "U-LC"))  # investigator，无 sign:approve
        ctx = _ctx(lc, test_db)
        with pytest.raises(ForbiddenError):
            await routes.sign_case(
                request=None, case_id="DEV-ANY",
                body=SignatureRequest(signature_meaning="approve", password="rca-demo-2025"),
                db=test_db, auth=ctx,
            )

    async def test_case_not_found_for_submit(self, test_db: AsyncSession, seed_db):
        from shared.exceptions import NotFoundError

        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        with pytest.raises(NotFoundError):
            await routes.sign_case(
                request=None, case_id="DEV-NOT-EXIST",
                body=SignatureRequest(signature_meaning="submit", password="rca-demo-2025"),
                db=test_db, auth=ctx,
            )

    async def test_submit_requires_reviewing(self, test_db: AsyncSession, seed_db):
        from shared.exceptions import ValidationError

        c = await _case(test_db, id="DEV-SG-1", status="pending")
        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        with pytest.raises(ValidationError):
            await routes.sign_case(
                request=None, case_id=c.id,
                body=SignatureRequest(signature_meaning="submit", password="rca-demo-2025"),
                db=test_db, auth=ctx,
            )

    async def test_duplicate_submit(self, test_db: AsyncSession, seed_db):
        from shared.exceptions import ConflictError

        c = await self._reviewing_case(test_db, "DEV-SG-2")
        add_sig(test_db, f"SIG-{c.id}-SUBMIT-U-LC", c.id, "U-LC", "提交签名")
        await test_db.commit()
        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        with pytest.raises(ConflictError):
            await routes.sign_case(
                request=None, case_id=c.id,
                body=SignatureRequest(signature_meaning="submit", password="rca-demo-2025"),
                db=test_db, auth=ctx,
            )

    async def test_submit_success(self, test_db: AsyncSession, seed_db):
        c = await self._reviewing_case(test_db, "DEV-SG-3")
        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        r = await routes.sign_case(
            request=None, case_id=c.id,
            body=SignatureRequest(signature_meaning="submit", password="rca-demo-2025"),
            db=test_db, auth=ctx,
        )
        assert r.signature_meaning == "提交签名" and r.signer_id == "U-LC"

    async def test_approve_requires_reviewing(self, test_db: AsyncSession, seed_db):
        from shared.exceptions import ValidationError

        c = await _case(test_db, id="DEV-SG-4", status="pending")
        jw = (await _user(test_db, "U-JW"))  # qa_reviewer，有 sign:approve
        ctx = _ctx(jw, test_db)
        with pytest.raises(ValidationError):
            await routes.sign_case(
                request=None, case_id=c.id,
                body=SignatureRequest(signature_meaning="approve", password="rca-demo-2025"),
                db=test_db, auth=ctx,
            )

    async def test_approve_need_submit_first(self, test_db: AsyncSession, seed_db):
        from shared.exceptions import ValidationError

        c = await self._reviewing_case(test_db, "DEV-SG-5")
        jw = (await _user(test_db, "U-JW"))
        ctx = _ctx(jw, test_db)
        with pytest.raises(ValidationError):
            await routes.sign_case(
                request=None, case_id=c.id,
                body=SignatureRequest(signature_meaning="approve", password="rca-demo-2025"),
                db=test_db, auth=ctx,
            )

    async def test_approve_same_user_rejected(self, test_db: AsyncSession, seed_db):
        from shared.exceptions import ValidationError

        c = await self._reviewing_case(test_db, "DEV-SG-6")
        # U-JW 自己先提交 → 再批准（同人双签拒绝，命中 754 分支）
        add_sig(test_db, f"SIG-{c.id}-SUBMIT-U-JW", c.id, "U-JW", "提交签名")
        await test_db.commit()
        jw = (await _user(test_db, "U-JW"))
        ctx = _ctx(jw, test_db)
        with pytest.raises(ValidationError):
            await routes.sign_case(
                request=None, case_id=c.id,
                body=SignatureRequest(signature_meaning="approve", password="rca-demo-2025"),
                db=test_db, auth=ctx,
            )

    async def test_approve_success(self, test_db: AsyncSession, seed_db):
        c = await self._reviewing_case(test_db, "DEV-SG-7")
        add_sig(test_db, f"SIG-{c.id}-SUBMIT-U-LC", c.id, "U-LC", "提交签名")
        await test_db.commit()
        jw = (await _user(test_db, "U-JW"))
        ctx = _ctx(jw, test_db)
        r = await routes.sign_case(
            request=None, case_id=c.id,
            body=SignatureRequest(signature_meaning="approve", password="rca-demo-2025"),
            db=test_db, auth=ctx,
        )
        assert r.signature_meaning == "批准签名"
        fresh = (await test_db.execute(
            select(Deviation).where(Deviation.id == c.id)
        )).scalar_one()
        assert fresh.status == "closed" and fresh.progress == 100

    async def test_approve_integrity_error(self, test_db: AsyncSession, seed_db):
        from shared.exceptions import ConflictError

        c = await self._reviewing_case(test_db, "DEV-SG-8")
        add_sig(test_db, f"SIG-{c.id}-SUBMIT-U-LC", c.id, "U-LC", "提交签名")
        # 预置相同 sig_id 的批准签名 → flush 时主键冲突 → 409
        add_sig(test_db, f"SIG-{c.id}-APPROVE-U-JW", c.id, "U-JW", "批准签名")
        await test_db.commit()
        jw = (await _user(test_db, "U-JW"))
        ctx = _ctx(jw, test_db)
        with pytest.raises(ConflictError):
            await routes.sign_case(
                request=None, case_id=c.id,
                body=SignatureRequest(signature_meaning="approve", password="rca-demo-2025"),
                db=test_db, auth=ctx,
            )

    async def test_closed_case_rejected(self, test_db: AsyncSession, seed_db):
        from shared.exceptions import ValidationError

        c = await _case(test_db, id="DEV-SG-9", status="closed")
        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        with pytest.raises(ValidationError):
            await routes.sign_case(
                request=None, case_id=c.id,
                body=SignatureRequest(signature_meaning="submit", password="rca-demo-2025"),
                db=test_db, auth=ctx,
            )


def add_sig(db, sig_id, case_id, signer_id, meaning):
    db.add(
        SignatureRecord(
            id=sig_id, case_id=case_id, signer_id=signer_id,
            signer_role="investigator" if signer_id == "U-LC" else "qa_reviewer",
            signature_meaning=meaning,
            certificate_fingerprint="sha256:test",
        )
    )


# ============================================================
#  get_kpi —— 实算各指标
# ============================================================
class TestGetKpi:
    async def test_compute_kpis(self, test_db: AsyncSession, seed_db):
        now = datetime.now(UTC)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        last_month = (month_start - timedelta(days=1)).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        # 本月 closed + overdue 案例
        await _case(
            test_db, id="DEV-KPI-1", severity="major", status="closed",
            discovered_at=now - timedelta(days=5),
            closed_at=now - timedelta(days=1),
            updated_at=now - timedelta(days=1),
            deadline=now - timedelta(days=2),
        )
        # 本月 open + 逾期
        await _case(
            test_db, id="DEV-KPI-2", severity="major", status="investigating",
            discovered_at=now - timedelta(days=3), deadline=now - timedelta(days=1),
        )
        # 本月 open + 未逾期
        await _case(
            test_db, id="DEV-KPI-3", severity="minor", status="pending",
            discovered_at=now - timedelta(days=1), deadline=now + timedelta(days=10),
        )
        # 上月 closed（趋势）
        await _case(
            test_db, id="DEV-KPI-4", severity="major", status="closed",
            discovered_at=last_month + timedelta(days=3),
            closed_at=(last_month + timedelta(days=5)),
            updated_at=(last_month + timedelta(days=5)),
            deadline=(last_month + timedelta(days=10)),
        )
        lc = (await _user(test_db, "U-LC"))
        # capa_overdue_rate 现按 CAPA 实体口径（3.2-1 对齐）：2 条 open CAPA、1 条逾期
        test_db.add(Capa(
            id="CAPA-KPI-1", case_id="DEV-KPI-2", title="逾期 CAPA",
            severity="major", responsibility="生产部", status="implemented",
            due_date=now - timedelta(days=1), created_at=now,
        ))
        test_db.add(Capa(
            id="CAPA-KPI-2", case_id="DEV-KPI-2", title="未逾期 CAPA",
            severity="minor", responsibility="QA", status="pending",
            due_date=now + timedelta(days=10), created_at=now,
        ))
        await test_db.commit()
        ctx = _ctx(lc, test_db)
        r = await routes.get_kpi(request=None, db=test_db, _=ctx)
        # 未解决 = 非 closed（DEV-KPI-2, DEV-KPI-3）
        assert r.unsolved_count == 2
        assert r.major_count_this_month >= 1
        assert r.capa_overdue_rate == 0.5
        assert isinstance(r.avg_cycle_days, float)
        assert isinstance(r.trend_unsolved_delta, int)
        assert isinstance(r.trend_major_delta, int)
        assert isinstance(r.trend_cycle_delta, float)
        assert isinstance(r.trend_capa_delta, float)


# ============================================================
#  get_trend —— SQLite 不支持 to_char/interval，用 Mock db 覆盖聚合分支
# ============================================================
class TestGetTrend:
    @staticmethod
    def _fake_db(rows):
        class _FakeResult:
            def __init__(self, all_):
                self._all = all_

            def all(self):
                return self._all

        async def _execute(stmt, *a, **k):
            return _FakeResult(rows)

        return SimpleNamespace(execute=_execute)

    async def test_aggregation_branches(self):
        rows = [
            SimpleNamespace(date="2026-08-01", case_type="microbe", cnt=2),
            SimpleNamespace(date="2026-08-01", case_type="em", cnt=1),
            SimpleNamespace(date="2026-08-01", case_type="impurity", cnt=1),
            SimpleNamespace(date="2026-08-01", case_type="particle", cnt=1),
            SimpleNamespace(date="2026-08-02", case_type="signature", cnt=1),
            SimpleNamespace(date="2026-08-02", case_type="hvac", cnt=1),
            SimpleNamespace(date="2026-08-02", case_type="clean", cnt=1),
            SimpleNamespace(date="2026-08-02", case_type=None, cnt=1),
            SimpleNamespace(date="2026-08-03", case_type="unknown-type", cnt=1),
        ]
        db = self._fake_db(rows)
        pts = await routes.get_trend(request=None, db=db)
        by_date = {p.date: p for p in pts}
        assert by_date["2026-08-01"].microbe == 3
        assert by_date["2026-08-01"].impurity == 1
        assert by_date["2026-08-01"].particle == 1
        assert by_date["2026-08-02"].signature == 1
        assert by_date["2026-08-02"].hvac == 1
        assert by_date["2026-08-02"].cleaning == 1


# ============================================================
#  list_source_systems
# ============================================================
class TestListSourceSystems:
    async def test_list(self, test_db: AsyncSession, seed_db):
        test_db.add_all(
            [
                SourceSystem(code="LIMS", label="实验室信息管理"),
                SourceSystem(code="EMS", label="环境监测"),
                SourceSystem(code="MES", label="生产执行"),
            ]
        )
        await test_db.commit()
        lc = (await _user(test_db, "U-LC"))
        ctx = _ctx(lc, test_db)
        r = await routes.list_source_systems(request=None, db=test_db, _=ctx)
        codes = [s.code for s in r]
        assert codes == ["EMS", "LIMS", "MES"]


# ============================================================
#  消融实验 E1 —— is_unique_violation 判别式各信号必要性
#  （真 PG15 探针实证：本地化消息下仅类名信号可判；SQLite 仅消息可判）
# ============================================================
class TestUniqueViolationClassifier:
    def test_pg_unique_via_class_name_despite_localized_message(self):
        """消融：PG lc_messages 非 English —— 消息不含 "unique constraint"、
        diag.constraint_name 为 None，唯一可用信号是异常类名。"""
        import psycopg2.errors

        from case_service.routes import is_unique_violation

        e = psycopg2.errors.UniqueViolation('重复键违反唯一约束 "deviations_pkey"')
        assert is_unique_violation(e, Exception("任意包装消息")) is True

    def test_sqlite_unique_via_message_only(self):
        """消融：SQLite 异常类是普通 IntegrityError（类名信号缺席）——
        唯一可用信号是消息文本，证明消息项必要。"""
        import sqlite3

        from case_service.routes import is_unique_violation

        e = sqlite3.IntegrityError("UNIQUE constraint failed: deviations.id")
        assert is_unique_violation(e, e) is True

    def test_sqlite_fk_not_unique(self):
        import sqlite3

        from case_service.routes import is_unique_violation

        e = sqlite3.IntegrityError("FOREIGN KEY constraint failed")
        assert is_unique_violation(e, e) is False

    def test_pg_fk_not_unique(self):
        import psycopg2.errors

        from case_service.routes import is_unique_violation

        e = psycopg2.errors.ForeignKeyViolation(
            'insert or update on table "deviations" violates foreign key '
            'constraint "deviations_investigator_id_fkey"'
        )
        assert is_unique_violation(e, Exception()) is False

    def test_sqlalchemy_wrapped_orig_shape(self):
        """路由实际捕获形态：sqlalchemy.exc.IntegrityError.orig = DBAPI 异常。"""
        import sqlite3

        from case_service.routes import is_unique_violation

        orig = sqlite3.IntegrityError("UNIQUE constraint failed: deviations.id")
        sa_err = SAIntegrityError("INSERT INTO deviations ...", None, orig)
        assert is_unique_violation(sa_err.orig, sa_err) is True

    def test_none_orig_falls_back_to_message(self):
        from case_service.routes import is_unique_violation

        assert is_unique_violation(None, Exception("UNIQUE constraint failed: t.id")) is True
