"""audit_service.routes 端点测试 — 查询/验证/写入/导出/feature flag 全覆盖

本地 fixture 仿照 conftest.case_app_client 的写法：
create_app("audit-service", enable_metrics=False) + include_router(audit_router, prefix="/api/v1")
+ dependency_overrides 覆盖 get_db / get_db_from_auth（复用 conftest 的 test_engine/seed_db），
并额外覆盖 get_auth_context —— 按邮箱构造 AuthContext 绕过 JWT，便于在各角色间切换
（admin=audit:read/export+system:configure，investigator=仅 audit:read，qa=audit:read/export，
operator=无任何权限，测试内动态创建）。
"""
from __future__ import annotations

import csv
import io
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audit_service.routes import _csv_safe, _export_filename
from shared.audit import append_audit_log
from shared.models import AuditLog, User
from shared.security import hash_password

ADMIN_EMAIL = "admin@biopharma.com"  # 全权限：audit:read/export + system:configure
INVESTIGATOR_EMAIL = "lisa.chen@biopharma.com"  # 仅 audit:read
QA_EMAIL = "james.wilson@biopharma.com"  # audit:read + audit:export
OPERATOR_EMAIL = "op-audit-test@biopharma.com"  # 无任何权限（本文件内创建）

_LOGS_PATH = "/api/v1/audit/logs"
_VERIFY_PATH = "/api/v1/audit/verify"
_EXPORT_PATH = "/api/v1/audit/export"
_FLAGS_PATH = "/api/v1/audit/flags"
_FLAGS_RELOAD_PATH = "/api/v1/audit/flags/reload"

_CSV_HEADER = [
    "seq", "log_id", "timestamp", "operator", "action", "target",
    "detail", "ip_address", "session_id", "evidence_refs",
    "ai_model", "ai_prompt_hash", "ai_output_hash", "trace_id",
    "prev_hash", "current_hash",
]

_EXPORT_CSV_DISPOSITION = r'attachment; filename="audit_export_\d{8}T\d{6}Z\.csv"'
_EXPORT_JSON_DISPOSITION = r'attachment; filename="audit_export_\d{8}T\d{6}Z\.json"'


# ============================================================
#  Fixture
# ============================================================
@pytest_asyncio.fixture
async def operator_user(test_db: AsyncSession):
    """无任何权限的 operator 用户 —— 用于 403 分支"""
    user = User(
        id="U-OP-AUDIT",
        email=OPERATOR_EMAIL,
        name="Audit Op",
        role="operator",
        password_hash=hash_password("op-audit-2025"),
        is_active=True,
    )
    test_db.add(user)
    await test_db.commit()
    return user


@pytest_asyncio.fixture
async def audit_client_factory(test_engine, seed_db):
    """audit-service 测试客户端工厂 —— 按邮箱构造认证上下文（角色随种子用户）"""
    from audit_service.routes import router as audit_router
    from shared.auth_middleware import AuthContext, get_auth_context, get_db_from_auth
    from shared.database import get_db
    from shared.security import ROLE_PERMISSIONS
    from shared.service_base import create_app

    session_factory = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    @asynccontextmanager
    async def _client_for(email: str) -> AsyncIterator[AsyncClient]:
        # 用户在客户端创建时才查询 —— 允许测试内先动态建用户（如 operator_user）
        async with session_factory() as lookup:
            result = await lookup.execute(select(User).where(User.email == email))
            user = result.scalar_one()

        def override_auth_context() -> AuthContext:
            return AuthContext(
                user=user,
                session_id="test-session",
                ip_address="127.0.0.1",
                permissions=ROLE_PERMISSIONS.get(user.role, set()),
                db=None,  # type: ignore[arg-type]  # get_db_from_auth 已被覆盖，端点不会读取该字段
            )

        app = create_app("audit-service", enable_metrics=False)
        app.include_router(audit_router, prefix="/api/v1")
        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_db_from_auth] = override_get_db
        app.dependency_overrides[get_auth_context] = override_auth_context

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
        app.dependency_overrides.clear()

    return _client_for


async def _add_log(
    db: AsyncSession,
    *,
    operator: str = "seed@biopharma.com",
    action: str = "SEED_ACT",
    target: str = "DEV-SEED-001",
    detail: str | None = None,
    evidence_refs: list[str] | None = None,
    ai_model: str | None = None,
) -> AuditLog:
    """经 append_audit_log 造一条链上日志（自动提交）"""
    log = await append_audit_log(
        db,
        operator=operator,
        action=action,
        target=target,
        detail=detail,
        evidence_refs=evidence_refs or [],
        ai_model=ai_model,
    )
    await db.commit()
    return log


# ============================================================
#  GET /audit/logs — 查询 + 过滤 + 分页 + 权限
# ============================================================
class TestListLogs:
    async def test_list_empty_db(self, audit_client_factory):
        """空库 → 空分页响应（默认 page=1/page_size=50）"""
        async with audit_client_factory(INVESTIGATOR_EMAIL) as client:
            resp = await client.get(_LOGS_PATH)
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0
        assert data["page"] == 1
        assert data["page_size"] == 50

    async def test_list_sorted_by_seq_desc(self, test_db, audit_client_factory):
        """默认按 seq 倒序返回，字段完整序列化"""
        for i in range(3):
            await _add_log(test_db, target=f"DEV-LIST-{i:03d}", detail=f"d{i}")
        async with audit_client_factory(INVESTIGATOR_EMAIL) as client:
            resp = await client.get(_LOGS_PATH)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        assert [item["seq"] for item in data["items"]] == [3, 2, 1]
        first = data["items"][0]
        assert first["log_id"].startswith("LOG-")
        assert first["operator"] == "seed@biopharma.com"
        assert first["action"] == "SEED_ACT"
        assert first["target"] == "DEV-LIST-002"
        assert first["prev_hash"] == data["items"][1]["current_hash"]  # 链衔接
        assert data["items"][-1]["prev_hash"] == "0" * 64  # 链头接创世哈希
        assert first["current_hash"].startswith("sha256:")

    async def test_filter_by_case_id_prefix(self, test_db, audit_client_factory):
        """case_id 按前缀 like 匹配 target"""
        await _add_log(test_db, target="DEV-A-001")
        await _add_log(test_db, target="DEV-A-002")
        await _add_log(test_db, target="DEV-B-777")
        async with audit_client_factory(INVESTIGATOR_EMAIL) as client:
            resp = await client.get(_LOGS_PATH, params={"case_id": "DEV-A"})
            assert resp.status_code == 200
            assert resp.json()["total"] == 2
            resp = await client.get(_LOGS_PATH, params={"case_id": "DEV-B"})
            assert resp.json()["total"] == 1
            resp = await client.get(_LOGS_PATH, params={"case_id": "DEV-NONE"})
            assert resp.json()["total"] == 0

    async def test_filter_by_operator(self, test_db, audit_client_factory):
        await _add_log(test_db, operator="alice@biopharma.com")
        await _add_log(test_db, operator="alice@biopharma.com")
        await _add_log(test_db, operator="bob@biopharma.com")
        async with audit_client_factory(INVESTIGATOR_EMAIL) as client:
            resp = await client.get(_LOGS_PATH, params={"operator": "alice@biopharma.com"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert all(item["operator"] == "alice@biopharma.com" for item in data["items"])

    async def test_filter_by_action(self, test_db, audit_client_factory):
        await _add_log(test_db, action="CREATE")
        await _add_log(test_db, action="CREATE")
        await _add_log(test_db, action="UPDATE")
        async with audit_client_factory(INVESTIGATOR_EMAIL) as client:
            resp = await client.get(_LOGS_PATH, params={"action": "UPDATE"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["action"] == "UPDATE"

    async def test_filter_by_time_window(self, test_db, audit_client_factory):
        """start/end 边界：过去窗口全命中、未来/过去端点空命中、组合窗口命中"""
        await _add_log(test_db)
        await _add_log(test_db)
        now = datetime.now(UTC)
        past = (now - timedelta(hours=1)).isoformat()
        future = (now + timedelta(hours=1)).isoformat()
        async with audit_client_factory(INVESTIGATOR_EMAIL) as client:
            resp = await client.get(_LOGS_PATH, params={"start": past})
            assert resp.json()["total"] == 2
            resp = await client.get(_LOGS_PATH, params={"start": future})
            assert resp.json()["total"] == 0
            resp = await client.get(_LOGS_PATH, params={"end": future})
            assert resp.json()["total"] == 2
            resp = await client.get(_LOGS_PATH, params={"end": past})
            assert resp.json()["total"] == 0
            resp = await client.get(_LOGS_PATH, params={"start": past, "end": future})
            assert resp.json()["total"] == 2

    async def test_pagination(self, test_db, audit_client_factory):
        """page/page_size 生效：第 2 页只剩最旧一条"""
        for i in range(3):
            await _add_log(test_db, detail=f"page-{i}")
        async with audit_client_factory(INVESTIGATOR_EMAIL) as client:
            resp = await client.get(_LOGS_PATH, params={"page": 2, "page_size": 2})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        assert data["page"] == 2
        assert data["page_size"] == 2
        assert [item["seq"] for item in data["items"]] == [1]

    async def test_page_beyond_range_returns_empty(self, test_db, audit_client_factory):
        await _add_log(test_db)
        async with audit_client_factory(INVESTIGATOR_EMAIL) as client:
            resp = await client.get(_LOGS_PATH, params={"page": 99})
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 1

    async def test_invalid_pagination_rejected(self, audit_client_factory):
        """page<1 / page_size>500 → 422"""
        async with audit_client_factory(INVESTIGATOR_EMAIL) as client:
            resp_page = await client.get(_LOGS_PATH, params={"page": 0})
            resp_size = await client.get(_LOGS_PATH, params={"page_size": 501})
        assert resp_page.status_code == 422
        assert resp_size.status_code == 422

    async def test_forbidden_without_audit_read(self, audit_client_factory, operator_user):
        """operator 无 audit:read → 403"""
        async with audit_client_factory(OPERATOR_EMAIL) as client:
            resp = await client.get(_LOGS_PATH)
        assert resp.status_code == 403
        assert resp.json()["code"] == "E4030"


# ============================================================
#  GET /audit/verify — hash 链完整性
# ============================================================
class TestVerifyChain:
    async def test_verify_intact_chain(self, test_db, audit_client_factory):
        for i in range(3):
            await _add_log(test_db, detail=f"v{i}")
        async with audit_client_factory(INVESTIGATOR_EMAIL) as client:
            resp = await client.get(_VERIFY_PATH)
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is True
        assert data["total_logs"] == 3
        assert datetime.fromisoformat(data["verified_at"]) is not None

    async def test_verify_empty_db(self, audit_client_factory):
        """空库 → valid=true 且 total_logs=0（count 为 None 分支）"""
        async with audit_client_factory(INVESTIGATOR_EMAIL) as client:
            resp = await client.get(_VERIFY_PATH)
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is True
        assert data["total_logs"] == 0

    async def test_verify_detects_tampering(self, test_db, audit_client_factory):
        """直改 DB 绕过应用层 → verify 返回 valid=false"""
        await _add_log(test_db, detail="原始")
        await _add_log(test_db, detail="第二条")
        await test_db.execute(update(AuditLog).where(AuditLog.seq == 1).values(detail="篡改"))
        await test_db.commit()
        async with audit_client_factory(INVESTIGATOR_EMAIL) as client:
            resp = await client.get(_VERIFY_PATH)
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is False
        assert data["total_logs"] == 2

    async def test_forbidden_without_audit_read(self, audit_client_factory, operator_user):
        async with audit_client_factory(OPERATOR_EMAIL) as client:
            resp = await client.get(_VERIFY_PATH)
        assert resp.status_code == 403


# ============================================================
#  POST /audit/logs — 写入（仅 admin，operator 强制归因）
# ============================================================
class TestWriteLog:
    async def test_declared_operator_mismatch_forced_attribution(self, audit_client_factory):
        """声明 operator 与认证身份不符 → 强制归因 + detail 记录声明值"""
        async with audit_client_factory(ADMIN_EMAIL) as client:
            resp = await client.post(
                _LOGS_PATH,
                json={
                    "operator": "victim@biopharma.com",
                    "action": "FAKE_ACTION",
                    "target": "DEV-FAKE",
                    "detail": "伪造署名尝试",
                },
            )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["operator"] == ADMIN_EMAIL
        assert "declared_operator=victim@biopharma.com" in data["detail"]
        assert "伪造署名尝试" in data["detail"]
        assert data["ip_address"] == "127.0.0.1"  # 未传 ip → 回退认证上下文
        assert data["session_id"] == "test-session"
        assert data["seq"] == 1
        assert data["prev_hash"] == "0" * 64
        assert data["current_hash"].startswith("sha256:")
        assert data["log_id"].startswith("LOG-")

    async def test_operator_matching_auth_keeps_detail(self, audit_client_factory):
        """声明 operator 与认证身份一致 → detail 原样保留"""
        async with audit_client_factory(ADMIN_EMAIL) as client:
            resp = await client.post(
                _LOGS_PATH,
                json={
                    "operator": ADMIN_EMAIL,
                    "action": "HONEST_ACT",
                    "detail": "正常记录",
                    "ip_address": "10.0.0.9",
                    "evidence_refs": ["E-001", "E-002"],
                    "ai_model": "test-model",
                },
            )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["operator"] == ADMIN_EMAIL
        assert data["detail"] == "正常记录"
        assert "declared_operator" not in data["detail"]
        assert data["ip_address"] == "10.0.0.9"  # 显式传入优先
        assert data["evidence_refs"] == ["E-001", "E-002"]
        assert data["ai_model"] == "test-model"

    async def test_minimal_body_defaults(self, audit_client_factory):
        """仅必填字段 → detail 空串、target/evidence_refs 缺省"""
        async with audit_client_factory(ADMIN_EMAIL) as client:
            resp = await client.post(_LOGS_PATH, json={"operator": ADMIN_EMAIL, "action": "MIN"})
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["detail"] == ""
        assert data["target"] is None
        assert data["evidence_refs"] == []

    async def test_forbidden_for_non_admin(self, audit_client_factory):
        """investigator 无 system:configure → 403"""
        async with audit_client_factory(INVESTIGATOR_EMAIL) as client:
            resp = await client.post(_LOGS_PATH, json={"operator": "x@y.com", "action": "A"})
        assert resp.status_code == 403
        assert resp.json()["code"] == "E4030"

    async def test_validation_error_missing_action(self, audit_client_factory):
        """缺必填 action → 422"""
        async with audit_client_factory(ADMIN_EMAIL) as client:
            resp = await client.post(_LOGS_PATH, json={"operator": "x@y.com"})
        assert resp.status_code == 422
        assert resp.json()["code"] == "E4220"


# ============================================================
#  GET /audit/export — CSV/JSON 流式导出
# ============================================================
class TestExportLogs:
    async def test_export_csv(self, test_db, audit_client_factory):
        """CSV 导出：表头/字段/拼接/Disposition 全量校验"""
        await _add_log(
            test_db,
            operator="exporter@biopharma.com",
            action="EXPORT_ACT",
            target="DEV-EXP-001",
            detail="导出详情",
            evidence_refs=["E-001", "E-002"],
            ai_model="test-model",
        )
        async with audit_client_factory(QA_EMAIL) as client:
            resp = await client.get(_EXPORT_PATH, params={"format": "csv"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")
        assert re.fullmatch(_EXPORT_CSV_DISPOSITION, resp.headers["content-disposition"])
        rows = list(csv.reader(io.StringIO(resp.text)))
        assert len(rows) == 2
        assert rows[0] == _CSV_HEADER
        data = rows[1]
        assert data[0] == "1"
        assert data[1].startswith("LOG-")
        assert data[3] == "exporter@biopharma.com"
        assert data[4] == "EXPORT_ACT"
        assert data[5] == "DEV-EXP-001"
        assert data[6] == "导出详情"
        assert data[7] == ""  # ip 未传 → _csv_safe(None) 空串
        assert data[8] == ""  # session 未传 → 空串
        assert data[9] == "E-001|E-002"
        assert data[10] == "test-model"
        assert data[14] == "0" * 64
        assert data[15].startswith("sha256:")

    async def test_export_csv_formula_injection_escaped(self, test_db, audit_client_factory):
        """以 =/@ 开头的单元格前置单引号（CSV 公式注入防护）"""
        await _add_log(test_db, target="@evil-target", detail="=1+1")
        async with audit_client_factory(ADMIN_EMAIL) as client:
            resp = await client.get(_EXPORT_PATH, params={"format": "csv"})
        assert resp.status_code == 200
        rows = list(csv.reader(io.StringIO(resp.text)))
        assert len(rows) == 2
        assert rows[1][5] == "'@evil-target"
        assert rows[1][6] == "'=1+1"

    async def test_export_json(self, test_db, audit_client_factory):
        """JSON 导出：流式拼接合法 JSON，字段完整"""
        await _add_log(test_db, detail="第一条", evidence_refs=["E-1"], ai_model="m1")
        await _add_log(test_db, detail="第二条")
        async with audit_client_factory(QA_EMAIL) as client:
            resp = await client.get(_EXPORT_PATH, params={"format": "json"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        assert re.fullmatch(_EXPORT_JSON_DISPOSITION, resp.headers["content-disposition"])
        data = resp.json()
        assert data["exported_at"].startswith("audit_export_")
        assert data["exported_at"].endswith(".json")
        assert [log["seq"] for log in data["logs"]] == [1, 2]
        assert data["logs"][0]["detail"] == "第一条"
        assert data["logs"][0]["evidence_refs"] == ["E-1"]
        assert data["logs"][0]["ai_model"] == "m1"
        assert data["logs"][0]["current_hash"].startswith("sha256:")

    async def test_export_json_empty_db(self, audit_client_factory):
        """空库 JSON 导出 → logs 空数组"""
        async with audit_client_factory(QA_EMAIL) as client:
            resp = await client.get(_EXPORT_PATH, params={"format": "json"})
        assert resp.status_code == 200
        assert resp.json()["logs"] == []

    async def test_export_csv_empty_db_only_header(self, audit_client_factory):
        """空库 CSV 导出（默认 format=csv）→ 仅表头"""
        async with audit_client_factory(ADMIN_EMAIL) as client:
            resp = await client.get(_EXPORT_PATH)
        assert resp.status_code == 200
        assert list(csv.reader(io.StringIO(resp.text))) == [_CSV_HEADER]

    async def test_export_case_id_filter(self, test_db, audit_client_factory):
        """导出支持 case_id 前缀过滤"""
        await _add_log(test_db, target="DEV-A-001")
        await _add_log(test_db, target="DEV-B-001")
        async with audit_client_factory(QA_EMAIL) as client:
            resp = await client.get(_EXPORT_PATH, params={"case_id": "DEV-A"})
        assert resp.status_code == 200
        rows = list(csv.reader(io.StringIO(resp.text)))
        assert len(rows) == 2
        assert rows[1][5] == "DEV-A-001"

    async def test_export_time_filters(self, test_db, audit_client_factory):
        """导出支持 start/end 过滤（含无命中分支）"""
        await _add_log(test_db)
        now = datetime.now(UTC)
        past = (now - timedelta(hours=1)).isoformat()
        future = (now + timedelta(hours=1)).isoformat()
        async with audit_client_factory(QA_EMAIL) as client:
            resp = await client.get(_EXPORT_PATH, params={"format": "json", "start": future})
            assert resp.json()["logs"] == []
            resp = await client.get(_EXPORT_PATH, params={"format": "json", "end": past})
            assert resp.json()["logs"] == []
            resp = await client.get(_EXPORT_PATH, params={"format": "json", "start": past, "end": future})
            assert len(resp.json()["logs"]) == 1

    async def test_export_multi_chunk_streaming(self, test_db, audit_client_factory):
        """501 条 > 单窗 500 —— 覆盖 _iter_logs 游标分窗的循环继续分支"""
        now = datetime.now(UTC)
        test_db.add_all(
            [
                AuditLog(
                    log_id=f"LOG-BULK-{i:06d}",
                    seq=i,
                    operator="bulk@biopharma.com",
                    action="BULK_EXPORT",
                    target=f"DEV-BULK-{i:06d}",
                    detail=f"bulk row {i}",
                    prev_hash="0" * 64,
                    current_hash=f"sha256:{i:064d}",
                    created_at=now,
                )
                for i in range(1, 502)
            ]
        )
        await test_db.commit()
        async with audit_client_factory(ADMIN_EMAIL) as client:
            resp = await client.get(_EXPORT_PATH, params={"format": "json"})
        assert resp.status_code == 200
        logs = resp.json()["logs"]
        assert len(logs) == 501
        assert logs[0]["seq"] == 1
        assert logs[500]["seq"] == 501

    async def test_export_invalid_format_rejected(self, audit_client_factory):
        """format 不在 csv|json 枚举内 → 422"""
        async with audit_client_factory(ADMIN_EMAIL) as client:
            resp = await client.get(_EXPORT_PATH, params={"format": "xml"})
        assert resp.status_code == 422

    async def test_forbidden_without_audit_export(self, test_db, audit_client_factory):
        """investigator 无 audit:export → 403"""
        await _add_log(test_db)
        async with audit_client_factory(INVESTIGATOR_EMAIL) as client:
            resp = await client.get(_EXPORT_PATH, params={"format": "csv"})
        assert resp.status_code == 403


# ============================================================
#  Feature Flag 运维端点
# ============================================================
class TestFeatureFlags:
    async def test_list_flags(self, audit_client_factory):
        """audit:read 即可列出全部 flag"""
        async with audit_client_factory(INVESTIGATOR_EMAIL) as client:
            resp = await client.get(_FLAGS_PATH)
        assert resp.status_code == 200
        flags = resp.json()["flags"]
        assert isinstance(flags, dict)
        assert "skeptic_v2_enhanced" in flags
        assert "enabled" in flags["skeptic_v2_enhanced"]

    async def test_list_flags_forbidden_without_audit_read(self, audit_client_factory, operator_user):
        async with audit_client_factory(OPERATOR_EMAIL) as client:
            resp = await client.get(_FLAGS_PATH)
        assert resp.status_code == 403

    async def test_reload_flags(self, audit_client_factory):
        """admin 热重载 → status ok"""
        async with audit_client_factory(ADMIN_EMAIL) as client:
            resp = await client.post(_FLAGS_RELOAD_PATH)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    async def test_reload_flags_forbidden_for_non_admin(self, audit_client_factory):
        """investigator 无 system:configure → 403"""
        async with audit_client_factory(INVESTIGATOR_EMAIL) as client:
            resp = await client.post(_FLAGS_RELOAD_PATH)
        assert resp.status_code == 403


# ============================================================
#  端点函数直调 —— 补齐 await 恢复行的 coverage 记录
# ============================================================
class TestDirectEndpointCalls:
    """直调端点函数（仿 test_security_fixes.TestAuditWriteEndpoint 的既有做法）

    HTTP 路径下 Python 3.12 sys.monitoring 与 SQLAlchemy greenlet 交互会丢失
    "await 恢复后首条语句"的 LINE 事件（list_logs 的 76/79、verify_chain 的 101、
    write_log 的 140 —— 响应数据可证明这些行实际执行过）。直调同一函数可让
    coverage 正常记录，消除该伪缺失。
    """

    @staticmethod
    def _ctx(user: User):
        from shared.auth_middleware import AuthContext
        from shared.security import ROLE_PERMISSIONS

        return AuthContext(
            user=user,
            session_id="direct-call-session",
            ip_address="192.168.1.1",
            permissions=ROLE_PERMISSIONS.get(user.role, set()),
            db=None,  # type: ignore[arg-type]  # 直调时 db 参数显式传入，不会读取该字段
        )

    async def test_list_logs_direct(self, test_db, seed_db):
        await _add_log(test_db)
        await _add_log(test_db)
        from audit_service.routes import list_logs

        # Query 参数直调时默认值是 Query(...) 对象而非 None —— 必须显式传 None
        page = await list_logs(
            request=MagicMock(),
            case_id=None,
            operator=None,
            action=None,
            start=None,
            end=None,
            page=1,
            page_size=10,
            db=test_db,
            _=self._ctx(seed_db[0]),
        )
        assert page.total == 2
        assert [item.seq for item in page.items] == [2, 1]

    async def test_verify_chain_direct(self, test_db, seed_db):
        await _add_log(test_db)
        from audit_service.routes import verify_chain

        result = await verify_chain(request=MagicMock(), db=test_db, _=self._ctx(seed_db[0]))
        assert result["valid"] is True
        assert result["total_logs"] == 1

    async def test_write_log_direct(self, test_db, seed_db):
        from audit_service.routes import write_log
        from shared.schemas import AuditLogCreate

        admin = seed_db[2]
        body = AuditLogCreate(operator="someone-else@biopharma.com", action="DIRECT")
        log = await write_log(request=MagicMock(), body=body, db=test_db, auth=self._ctx(admin))
        assert log.operator == admin.email
        assert "declared_operator=someone-else@biopharma.com" in log.detail


# ============================================================
#  模块级辅助函数单元测试（同步，不加 asyncio 标记）
# ============================================================
class TestCsvSafeUnit:
    def test_none_and_empty_return_empty(self):
        assert _csv_safe(None) == ""
        assert _csv_safe("") == ""

    def test_formula_prefixes_escaped(self):
        for prefix in ("=", "+", "-", "@", "\t", "\r"):
            assert _csv_safe(f"{prefix}cmd") == f"'{prefix}cmd"

    def test_normal_values_unchanged(self):
        assert _csv_safe("normal-value") == "normal-value"
        assert _csv_safe("123") == "123"


class TestExportFilenameUnit:
    def test_filename_format(self):
        assert re.fullmatch(r"audit_export_\d{8}T\d{6}Z\.csv", _export_filename("csv"))
        assert re.fullmatch(r"audit_export_\d{8}T\d{6}Z\.json", _export_filename("json"))
