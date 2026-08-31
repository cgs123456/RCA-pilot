"""Evidence Service 路由测试 — 覆盖 evidence_service/routes.py 的端点与分支

覆盖：
- GET /evidence：多维过滤（case_id / source_system / min_confidence / keyword）+ 分页
- GET /evidence/{id}：详情 + 404
- GET /cases/{case_id}/timeline：时间线排序
- POST /evidence/fetch-from-source：Connector 编排（成功 / content_hash 去重 / 员工号推导 /
  未配置 URL / 未映射源 / 非法源 / HTTP 错误 / X-Service-Key）
- POST /evidence/{id}/embed 与 POST /evidence/embed-batch：embedding（skipped / 404 /
  无摘要 / LLM 异常 / 成功落库 + 审计）
- semantic_search / find_related_cases：keyword 降级 / 语义模式 / LLM 异常降级

说明：
1. conftest 没有 evidence_service 的 app client fixture —— 本文件仿照 case_app_client
   自建 fixture（create_app + include_router + dependency_overrides 覆盖
   get_db / get_db_from_auth / get_auth_context）。
2. /evidence/search 与 /evidence/related-cases 注册在 /evidence/{evidence_id} 之后，
   HTTP 请求会先命中路径参数路由（FastAPI 按注册顺序匹配）—— 这两个端点函数通过直接
   调用覆盖（与 tests/test_connectors.py 直接调用 fetch_from_source 的做法一致）。
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio
from fastapi import Depends
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from evidence_service.routes import (
    _collected_at_of,
    _derive_employee_id,
    _extract_items,
    _summary_of,
    embed_evidence,
    embed_evidence_batch,
    fetch_from_source,
    find_related_cases,
    get_evidence,
    get_timeline,
    list_evidence,
    semantic_search,
)
from shared.auth_middleware import AuthContext, get_auth_context, get_db_from_auth
from shared.database import get_db
from shared.exceptions import NotFoundError, ValidationError
from shared.llm_client import LLMError
from shared.models import AuditLog, Evidence, TimelineEvent, User
from shared.security import ROLE_PERMISSIONS

LISA_EMAIL = "lisa.chen@biopharma.com"  # investigator —— evidence:read + evidence:write
JAMES_EMAIL = "james.wilson@biopharma.com"  # qa_reviewer —— 仅 evidence:read
OPERATOR_EMAIL = "op.evidence@biopharma.com"  # operator —— 无任何权限


# ============================================================
# fixture —— evidence-service 测试客户端（仿 conftest.case_app_client）
# ============================================================
@pytest_asyncio.fixture
async def evidence_client_factory(test_engine, seed_db):
    """evidence-service 测试客户端工厂 —— 按邮箱注入 AuthContext（绕过真实 JWT）"""
    from evidence_service.routes import router as evidence_router
    from shared.service_base import create_app

    session_factory = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )

    async def override_get_db():
        async with session_factory() as session:
            yield session
            await session.commit()

    created: list[AsyncClient] = []

    def _make(email: str = LISA_EMAIL) -> AsyncClient:
        app = create_app("evidence-service", enable_metrics=False)
        app.include_router(evidence_router, prefix="/api/v1")

        async def override_get_auth_context() -> AsyncGenerator[AuthContext, None]:
            async with session_factory() as session:
                user = (await session.execute(select(User).where(User.email == email))).scalar_one()
                yield AuthContext(
                    user=user,
                    session_id="test-session",
                    ip_address="127.0.0.1",
                    permissions=ROLE_PERMISSIONS.get(user.role, set()),
                    db=session,
                )
                await session.commit()

        async def override_get_db_from_auth(
            ctx: AuthContext = Depends(get_auth_context),
        ) -> AsyncSession:
            return ctx.db

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_auth_context] = override_get_auth_context
        app.dependency_overrides[get_db_from_auth] = override_get_db_from_auth

        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        created.append(client)
        return client

    yield _make

    for client in created:
        await client.aclose()


@pytest_asyncio.fixture
async def evidence_client(evidence_client_factory) -> AsyncClient:
    """默认客户端 —— lisa.chen（investigator，具备 evidence:read/write）"""
    return evidence_client_factory()  # type: ignore[no-any-return]


@pytest.fixture
async def operator_user(test_db: AsyncSession, seed_db) -> User:
    """operator 用户 —— 无任何权限（403 测试用）"""
    user = User(
        id="U-OP-EV",
        email=OPERATOR_EMAIL,
        name="Operator EV",
        role="operator",
        is_active=True,
    )
    test_db.add(user)
    await test_db.commit()
    return user


@pytest.fixture
async def evidence_env(test_db: AsyncSession, seed_db) -> dict:
    """种子数据：3 条证据（两个案例）+ 3 条时间线事件（验证排序）"""
    rows = [
        Evidence(
            id="E-LIST-01",
            case_id="DEV-LIST-A",
            source_system="LMS",
            collected_at=datetime(2025, 8, 14, 10, 0, tzinfo=UTC),
            created_at=datetime(2025, 8, 14, 10, 0, tzinfo=UTC),  # 最新，保证 created_at desc 顺序确定
            summary="EMP-2187 更衣资质逾期",
            raw_data={"employee_id": "EMP-2187"},
            content_hash="sha256:list-1",
            confidence=4,
        ),
        Evidence(
            id="E-LIST-02",
            case_id="DEV-LIST-A",
            source_system="BMS",
            collected_at=datetime(2025, 8, 13, 9, 0, tzinfo=UTC),
            created_at=datetime(2025, 8, 13, 9, 0, tzinfo=UTC),
            summary="AHU-7 差压 470 Pa 超过预警值",
            raw_data={"ahu_id": "AHU-7"},
            content_hash="sha256:list-2",
            confidence=3,
        ),
        Evidence(
            id="E-LIST-03",
            case_id="DEV-LIST-B",
            source_system="EMS",
            collected_at=datetime(2025, 8, 12, 8, 0, tzinfo=UTC),
            created_at=datetime(2025, 8, 12, 8, 0, tzinfo=UTC),
            summary="Room-3B 沉降菌 22 CFU 超标",
            raw_data={"point_id": "Room-3B"},
            content_hash="sha256:list-3",
            confidence=2,
        ),
        TimelineEvent(
            id="T-EV-01",
            case_id="DEV-LIST-A",
            occurred_at=datetime(2025, 8, 13, 8, 0, tzinfo=UTC),
            source="BMS",
            title="AHU-7 差压异常",
            detail="470 Pa",
            severity="warning",
            evidence_id="E-LIST-02",
            is_anomaly=True,
            sort_order=2,
        ),
        TimelineEvent(
            id="T-EV-02",
            case_id="DEV-LIST-A",
            occurred_at=datetime(2025, 8, 13, 8, 0, tzinfo=UTC),
            source="LMS",
            title="资质核验",
            severity="info",
            sort_order=1,
        ),
        TimelineEvent(
            id="T-EV-03",
            case_id="DEV-LIST-A",
            occurred_at=datetime(2025, 8, 14, 12, 0, tzinfo=UTC),
            source="LIMS",
            title="检测启动",
            severity="info",
            sort_order=0,
        ),
    ]
    test_db.add_all(rows)
    await test_db.commit()
    return {"evidence": rows[:3], "timeline": rows[3:]}


# ============================================================
# 辅助构造
# ============================================================
def _connector_http_client(payload=None, *, error: Exception | None = None):
    """模拟 httpx.AsyncClient —— 返回指定 JSON 响应或抛指定异常"""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = payload
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    if error is not None:
        mock_client.get = AsyncMock(side_effect=error)
    else:
        mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return mock_client


def _mock_llm(*, supports: bool = True, embed=None, embed_batch=None, error=None):
    """模拟 shared.llm_client.llm_client 单例"""
    llm = MagicMock()
    llm.supports_embeddings = supports
    llm.embedding_model_name = "embedding-3"
    if error is not None:
        llm.embed_text = AsyncMock(side_effect=error)
        llm.embed_texts = AsyncMock(side_effect=error)
    else:
        llm.embed_text = AsyncMock(return_value=embed if embed is not None else [0.1, 0.2, 0.3])
        llm.embed_texts = AsyncMock(
            return_value=embed_batch if embed_batch is not None else [[0.1], [0.2]]
        )
    return llm


def _lms_evidence(eid: str, case_id: str, raw_data: dict) -> Evidence:
    """构造 LMS 证据（员工号推导测试用）"""
    return Evidence(
        id=eid,
        case_id=case_id,
        source_system="LMS",
        collected_at=datetime.now(UTC),
        summary="LMS 培训记录",
        raw_data=raw_data,
        content_hash=f"sha256:{eid}",
        confidence=3,
    )


# ============================================================
# GET /evidence —— 列表过滤 + 分页
# ============================================================
class TestListEvidence:
    async def test_list_no_filter_returns_all_sorted_by_collected_at_desc(
        self, evidence_client, evidence_env
    ):
        resp = await evidence_client.get("/api/v1/evidence")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["total"] == 3
        assert data["page"] == 1
        assert data["page_size"] == 50
        assert [it["id"] for it in data["items"]] == ["E-LIST-01", "E-LIST-02", "E-LIST-03"]

    async def test_list_filter_by_case_id(self, evidence_client, evidence_env):
        resp = await evidence_client.get("/api/v1/evidence", params={"case_id": "DEV-LIST-A"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert {it["id"] for it in data["items"]} == {"E-LIST-01", "E-LIST-02"}

    async def test_list_filter_by_source_system(self, evidence_client, evidence_env):
        resp = await evidence_client.get("/api/v1/evidence", params={"source_system": "BMS"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["id"] == "E-LIST-02"

    async def test_list_filter_by_min_confidence(self, evidence_client, evidence_env):
        resp = await evidence_client.get("/api/v1/evidence", params={"min_confidence": 3})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert {it["id"] for it in data["items"]} == {"E-LIST-01", "E-LIST-02"}

    async def test_list_filter_by_keyword(self, evidence_client, evidence_env):
        resp = await evidence_client.get("/api/v1/evidence", params={"keyword": "差压"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["id"] == "E-LIST-02"

    async def test_list_pagination(self, evidence_client, evidence_env):
        resp = await evidence_client.get(
            "/api/v1/evidence", params={"page": 2, "page_size": 2}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        assert data["page"] == 2
        assert data["page_size"] == 2
        assert [it["id"] for it in data["items"]] == ["E-LIST-03"]

    async def test_list_combined_filters(self, evidence_client, evidence_env):
        resp = await evidence_client.get(
            "/api/v1/evidence",
            params={"case_id": "DEV-LIST-A", "source_system": "BMS", "min_confidence": 3},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["id"] == "E-LIST-02"


# ============================================================
# GET /evidence/{id} —— 详情 + 404
# ============================================================
class TestGetEvidence:
    async def test_get_existing_returns_full_raw_data(self, evidence_client, evidence_env):
        resp = await evidence_client.get("/api/v1/evidence/E-LIST-01")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["id"] == "E-LIST-01"
        assert data["case_id"] == "DEV-LIST-A"
        assert data["source_system"] == "LMS"
        assert data["raw_data"] == {"employee_id": "EMP-2187"}
        assert data["content_hash"] == "sha256:list-1"
        assert data["confidence"] == 4

    async def test_get_missing_returns_404(self, evidence_client):
        resp = await evidence_client.get("/api/v1/evidence/E-NOPE")
        assert resp.status_code == 404
        assert resp.json()["code"] == "E4040"
        assert "不存在" in resp.json()["message"]

    async def test_search_route_shadowed_by_path_param_returns_404(self, evidence_client):
        """/evidence/search 注册在 /evidence/{evidence_id} 之后 —— HTTP 层被路径参数路由
        遮蔽，命中 get_evidence 并返回 404（若调整路由注册顺序需同步更新本测试）"""
        resp = await evidence_client.get("/api/v1/evidence/search", params={"q": "差压"})
        assert resp.status_code == 404
        assert "search" in resp.json()["message"]


# ============================================================
# GET /cases/{case_id}/timeline
# ============================================================
class TestTimeline:
    async def test_timeline_ordered_by_occurred_at_then_sort_order(self, evidence_client, evidence_env):
        resp = await evidence_client.get("/api/v1/cases/DEV-LIST-A/timeline")
        assert resp.status_code == 200, resp.text
        events = resp.json()
        assert len(events) == 3
        # 同一 occurred_at 按 sort_order 升序，再按时间升序
        assert [e["id"] for e in events] == ["T-EV-02", "T-EV-01", "T-EV-03"]
        assert events[0]["sort_order"] == 1
        assert events[2]["occurred_at"] > events[1]["occurred_at"]

    async def test_timeline_empty_for_unknown_case(self, evidence_client):
        resp = await evidence_client.get("/api/v1/cases/DEV-NOPE/timeline")
        assert resp.status_code == 200
        assert resp.json() == []


# ============================================================
# 权限校验 —— 403
# ============================================================
class TestPermissionDenied:
    async def test_operator_cannot_read_evidence(self, evidence_client_factory, operator_user):
        client = evidence_client_factory(email=OPERATOR_EMAIL)
        resp = await client.get("/api/v1/evidence")
        assert resp.status_code == 403
        assert resp.json()["code"] == "E4030"

    async def test_qa_reviewer_cannot_embed(self, evidence_client_factory, evidence_env):
        client = evidence_client_factory(email=JAMES_EMAIL)
        resp = await client.post("/api/v1/evidence/E-LIST-01/embed")
        assert resp.status_code == 403
        assert resp.json()["code"] == "E4030"

    async def test_qa_reviewer_cannot_fetch_from_source(self, evidence_client_factory):
        client = evidence_client_factory(email=JAMES_EMAIL)
        resp = await client.post(
            "/api/v1/evidence/fetch-from-source", params={"source": "BMS"}
        )
        assert resp.status_code == 403


# ============================================================
# 纯函数 —— _extract_items / _summary_of / _collected_at_of
# ============================================================
class TestExtractItems:
    def test_list_input_filters_non_dict_items(self):
        assert _extract_items([{"a": 1}, "junk", 42, {"b": 2}]) == [{"a": 1}, {"b": 2}]

    def test_dict_with_items_key(self):
        assert _extract_items({"status": "ok", "items": [{"a": 1}]}) == [{"a": 1}]

    def test_dict_with_employees_key(self):
        assert _extract_items({"employees": [{"id": "EMP-1"}]}) == [{"id": "EMP-1"}]

    def test_dict_with_known_key_not_list_falls_back_to_single(self):
        # items 键存在但值不是 list —— 回退为单对象响应
        assert _extract_items({"items": None, "total": 5}) == [{"items": None, "total": 5}]

    def test_dict_without_known_keys_returns_single(self):
        assert _extract_items({"foo": "bar"}) == [{"foo": "bar"}]

    def test_non_dict_non_list_returns_empty(self):
        assert _extract_items("plain string") == []
        assert _extract_items(42) == []


class TestSummaryOf:
    def test_prefers_summary_field(self):
        assert _summary_of("BMS", {"summary": "AHU-7 差压超标"}) == "[BMS] AHU-7 差压超标"

    def test_prefers_alert_field(self):
        assert _summary_of("EMS", {"alert": "沉降菌超标"}) == "[EMS] 沉降菌超标"

    def test_blank_summary_falls_to_next_key(self):
        result = _summary_of("LMS", {"summary": "   ", "course_name": "无菌更衣"})
        assert result == "[LMS] 无菌更衣"

    def test_fallback_joins_first_three_fields(self):
        result = _summary_of("MES", {"a": 1, "b": 2, "c": 3, "d": 4})
        assert result == "[MES] a=1, b=2, c=3"

    def test_fallback_empty_item(self):
        assert _summary_of("BMS", {}) == "[BMS] 证据条目"


class TestCollectedAtOf:
    def test_parses_iso_timestamp_with_z_suffix(self):
        dt = _collected_at_of({"timestamp": "2025-08-14T10:00:00Z"})
        assert dt == datetime(2025, 8, 14, 10, 0, tzinfo=UTC)

    def test_invalid_timestamp_falls_back_to_now(self):
        dt = _collected_at_of({"detected_at": "not-a-date"})
        assert dt.tzinfo is not None

    def test_no_timestamp_key_falls_back_to_now(self):
        dt = _collected_at_of({"foo": "bar"})
        assert dt.tzinfo is not None


# ============================================================
# _derive_employee_id —— LMS 员工号推导
# ============================================================
class TestDeriveEmployeeId:
    async def test_returns_none_without_case_id(self, test_db, seed_db):
        assert await _derive_employee_id(test_db, None) is None

    async def test_returns_none_when_no_lms_evidence(self, test_db, seed_db):
        assert await _derive_employee_id(test_db, "DEV-EMPTY") is None

    async def test_derives_from_top_level_key_with_strip(self, test_db, seed_db):
        test_db.add(_lms_evidence("E-DERIVE-1", "DEV-DERIVE", {"employee_id": "  EMP-2187  "}))
        await test_db.commit()
        assert await _derive_employee_id(test_db, "DEV-DERIVE") == "EMP-2187"

    async def test_skips_non_string_then_falls_to_emp_id(self, test_db, seed_db):
        test_db.add(
            _lms_evidence("E-DERIVE-2", "DEV-DERIVE", {"employee_id": 123, "emp_id": "EMP-0002"})
        )
        await test_db.commit()
        assert await _derive_employee_id(test_db, "DEV-DERIVE") == "EMP-0002"

    async def test_blank_and_nonstring_values_return_none(self, test_db, seed_db):
        test_db.add(
            _lms_evidence(
                "E-DERIVE-3", "DEV-DERIVE", {"employee_id": "   ", "employeeId": 99, "emp_id": ""}
            )
        )
        await test_db.commit()
        assert await _derive_employee_id(test_db, "DEV-DERIVE") is None

    async def test_derives_from_nested_dict(self, test_db, seed_db):
        test_db.add(
            _lms_evidence(
                "E-DERIVE-4", "DEV-DERIVE", {"note": "文本", "record": {"employeeId": "EMP-9999"}}
            )
        )
        await test_db.commit()
        assert await _derive_employee_id(test_db, "DEV-DERIVE") == "EMP-9999"

    async def test_blank_nested_value_returns_none(self, test_db, seed_db):
        test_db.add(_lms_evidence("E-DERIVE-5", "DEV-DERIVE", {"profile": {"employee_id": "   "}}))
        await test_db.commit()
        assert await _derive_employee_id(test_db, "DEV-DERIVE") is None


# ============================================================
# POST /evidence/fetch-from-source —— Connector 编排
# ============================================================
class TestFetchFromSource:
    async def test_fetch_bms_success_persists_and_audits(self, evidence_client, test_db, evidence_env):
        payload = {
            "status": "ok",
            "source": "BMS",
            "items": [
                {"ahu_id": "AHU-7", "area": "Room-3B", "summary": "AHU-7 差压 470 Pa"},
                {"ahu_id": "AHU-8", "area": "Room-3B"},
            ],
        }
        mock_client = _connector_http_client(payload)
        with patch("evidence_service.routes.httpx.AsyncClient", return_value=mock_client):
            resp = await evidence_client.post(
                "/api/v1/evidence/fetch-from-source",
                params={"source": "BMS", "case_id": "DEV-LIST-A"},
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "ok"
        assert data["source"] == "BMS"
        assert data["case_id"] == "DEV-LIST-A"
        assert data["fetched_count"] == 2
        assert data["persisted_count"] == 2
        assert data["triggered_by"] == LISA_EMAIL

        # 调用 URL 指向 BMS Connector 入口端点
        url = mock_client.get.call_args.args[0]
        assert "/bms/ahus" in url

        # 落库验证：新增 2 条 BMS 证据（content_hash 去重 ID）
        rows = (
            await test_db.execute(
                select(Evidence).where(Evidence.source_system == "BMS").where(
                    Evidence.case_id == "DEV-LIST-A"
                )
            )
        ).scalars().all()
        assert len(rows) == 3  # 种子 E-LIST-02 + 新增 2 条
        assert sum(1 for r in rows if r.id.startswith("E-BMS-")) == 2

        # 审计：EVIDENCE_FETCHED 已写入
        logs = (
            await test_db.execute(select(AuditLog).where(AuditLog.action == "EVIDENCE_FETCHED"))
        ).scalars().all()
        assert len(logs) == 1
        assert logs[0].operator == LISA_EMAIL
        assert "新增落库 2 条" in logs[0].detail

    async def test_fetch_same_payload_twice_dedups(self, evidence_client, evidence_env):
        payload = {"items": [{"point_id": "Room-3B", "exceedance_type": "cfu_action"}]}
        mock_client = _connector_http_client(payload)
        with patch("evidence_service.routes.httpx.AsyncClient", return_value=mock_client):
            resp1 = await evidence_client.post(
                "/api/v1/evidence/fetch-from-source", params={"source": "EMS"}
            )
            resp2 = await evidence_client.post(
                "/api/v1/evidence/fetch-from-source", params={"source": "EMS"}
            )

        assert resp1.status_code == 200
        assert resp2.status_code == 200
        assert resp1.json()["persisted_count"] == 1
        # 第二次拉取：content_hash 命中已有记录 → 全部跳过（幂等）
        assert resp2.json()["fetched_count"] == 1
        assert resp2.json()["persisted_count"] == 0

    async def test_fetch_list_payload_filters_non_dict_items(self, evidence_client, test_db, evidence_env):
        payload = [
            {"summary": "AHU-9 检修记录", "timestamp": "2025-08-14T10:00:00Z"},
            "junk",
            42,
        ]
        mock_client = _connector_http_client(payload)
        with patch("evidence_service.routes.httpx.AsyncClient", return_value=mock_client):
            resp = await evidence_client.post(
                "/api/v1/evidence/fetch-from-source", params={"source": "BMS"}
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["fetched_count"] == 1
        assert data["persisted_count"] == 1

        # timestamp 字段被解析为 collected_at；summary 字段进入摘要
        # （SQLite 读回 datetime 会丢 tzinfo，比较前统一去掉）
        new_rows = (
            await test_db.execute(select(Evidence).where(Evidence.id.like("E-BMS-%")))
        ).scalars().all()
        assert len(new_rows) == 1
        assert new_rows[0].summary == "[BMS] AHU-9 检修记录"
        assert new_rows[0].collected_at.replace(tzinfo=None) == datetime(2025, 8, 14, 10, 0)

    async def test_fetch_lms_passes_derived_employee_id(self, evidence_client, test_db, seed_db):
        test_db.add(_lms_evidence("E-LMS-SEED", "DEV-LMS", {"employee_id": "EMP-2187"}))
        await test_db.commit()

        payload = {"employees": [{"employee_id": "EMP-2187", "course_name": "无菌更衣"}]}
        mock_client = _connector_http_client(payload)
        with patch("evidence_service.routes.httpx.AsyncClient", return_value=mock_client):
            resp = await evidence_client.post(
                "/api/v1/evidence/fetch-from-source",
                params={"source": "LMS", "case_id": "DEV-LMS"},
            )

        assert resp.status_code == 200, resp.text
        assert resp.json()["persisted_count"] == 1
        # 从已有 LMS 证据推导员工号并作为查询参数传给 Connector
        call_kwargs = mock_client.get.call_args.kwargs
        assert call_kwargs["params"].get("employee_id") == "EMP-2187"
        assert "/lms/cert-status" in mock_client.get.call_args.args[0]

    async def test_fetch_lms_without_case_id_no_employee_param(self, evidence_client, evidence_env):
        mock_client = _connector_http_client({"employees": []})
        with patch("evidence_service.routes.httpx.AsyncClient", return_value=mock_client):
            resp = await evidence_client.post(
                "/api/v1/evidence/fetch-from-source", params={"source": "LMS"}
            )

        assert resp.status_code == 200
        assert mock_client.get.call_args.kwargs["params"] == {}

    async def test_fetch_mes_with_case_id_param(self, evidence_client, evidence_env):
        payload = {"items": [{"anomaly_type": "inner_glove_not_covered"}]}
        mock_client = _connector_http_client(payload)
        with patch("evidence_service.routes.httpx.AsyncClient", return_value=mock_client):
            resp = await evidence_client.post(
                "/api/v1/evidence/fetch-from-source",
                params={"source": "MES", "case_id": "DEV-MES-9"},
            )

        assert resp.status_code == 200
        call_kwargs = mock_client.get.call_args.kwargs
        assert call_kwargs["params"].get("case_id") == "DEV-MES-9"
        assert "/mes/cctv/anomalies" in mock_client.get.call_args.args[0]

    async def test_fetch_sends_service_key_header_when_configured(self, evidence_client, monkeypatch):
        monkeypatch.setattr("shared.config.settings.service_api_key", "test-svc-key")
        mock_client = _connector_http_client({"items": []})
        with patch("evidence_service.routes.httpx.AsyncClient", return_value=mock_client):
            resp = await evidence_client.post(
                "/api/v1/evidence/fetch-from-source", params={"source": "BMS"}
            )

        assert resp.status_code == 200
        headers = mock_client.get.call_args.kwargs["headers"]
        assert headers.get("X-Service-Key") == "test-svc-key"

    async def test_fetch_unsupported_source_returns_502(self, evidence_client):
        resp = await evidence_client.post(
            "/api/v1/evidence/fetch-from-source", params={"source": "SCADA"}
        )
        assert resp.status_code == 502
        assert resp.json()["code"] == "E5003"
        assert "不支持的源系统" in resp.json()["message"]

    async def test_fetch_lims_not_mapped_returns_skipped(self, evidence_client):
        resp = await evidence_client.post(
            "/api/v1/evidence/fetch-from-source", params={"source": "LIMS"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "skipped"
        assert data["fetched_count"] == 0
        assert "LIMS" in data["message"]

    async def test_fetch_unconfigured_url_returns_skipped(self, evidence_client, monkeypatch):
        monkeypatch.setattr("shared.config.settings.lms_connector_url", "")
        resp = await evidence_client.post(
            "/api/v1/evidence/fetch-from-source", params={"source": "LMS"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "skipped"
        assert data["fetched_count"] == 0

    async def test_fetch_http_error_returns_502(self, evidence_client):
        mock_client = _connector_http_client(error=httpx.ConnectError("Connection refused"))
        with patch("evidence_service.routes.httpx.AsyncClient", return_value=mock_client):
            resp = await evidence_client.post(
                "/api/v1/evidence/fetch-from-source", params={"source": "BMS"}
            )

        assert resp.status_code == 502
        assert "BMS Connector 调用失败" in resp.json()["message"]


# ============================================================
# POST /evidence/{id}/embed —— 单条 embedding
# ============================================================
class TestEmbedEvidence:
    async def test_embed_missing_evidence_returns_404(self, evidence_client):
        resp = await evidence_client.post("/api/v1/evidence/E-NOPE/embed")
        assert resp.status_code == 404
        assert resp.json()["code"] == "E4040"

    async def test_embed_evidence_without_summary_returns_400(self, evidence_client, test_db, seed_db):
        test_db.add(
            Evidence(
                id="E-NOSUM",
                case_id="DEV-NOSUM",
                source_system="LIMS",
                collected_at=datetime.now(UTC),
                summary=None,
                raw_data={},
                content_hash="sha256:nosum",
                confidence=3,
            )
        )
        await test_db.commit()

        resp = await evidence_client.post("/api/v1/evidence/E-NOSUM/embed")
        assert resp.status_code == 400
        assert resp.json()["code"] == "E4000"
        assert "无摘要" in resp.json()["message"]

    async def test_embed_skipped_when_llm_not_configured(self, evidence_client, evidence_env):
        with patch("shared.llm_client.llm_client", _mock_llm(supports=False)):
            resp = await evidence_client.post("/api/v1/evidence/E-LIST-01/embed")

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "skipped"
        assert data["evidence_id"] == "E-LIST-01"
        assert data["embedding_model"] is None
        assert "未配置" in data["message"]

    async def test_embed_success_persists_and_audits(self, evidence_client, test_db, evidence_env):
        llm = _mock_llm(embed=[0.1, 0.2, 0.3])
        with patch("shared.llm_client.llm_client", llm):
            resp = await evidence_client.post("/api/v1/evidence/E-LIST-01/embed")

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "ok"
        assert data["evidence_id"] == "E-LIST-01"
        assert data["embedding_dim"] == 3
        assert data["embedding_model"] == "embedding-3"
        assert "embedded_at" in data
        llm.embed_text.assert_awaited_once_with("EMP-2187 更衣资质逾期")

        # DB 状态：embedding 已持久化
        ev = (await test_db.execute(select(Evidence).where(Evidence.id == "E-LIST-01"))).scalar_one()
        await test_db.refresh(ev)
        assert ev.embedding == [0.1, 0.2, 0.3]
        assert ev.embedding_model == "embedding-3"
        assert ev.embedded_at is not None

        # 审计：EVIDENCE_EMBEDDED 已写入
        logs = (
            await test_db.execute(select(AuditLog).where(AuditLog.action == "EVIDENCE_EMBEDDED"))
        ).scalars().all()
        assert len(logs) == 1
        assert logs[0].operator == LISA_EMAIL

    async def test_embed_llm_error_returns_400(self, evidence_client, evidence_env):
        llm = _mock_llm(error=LLMError("provider down"))
        with patch("shared.llm_client.llm_client", llm):
            resp = await evidence_client.post("/api/v1/evidence/E-LIST-01/embed")

        assert resp.status_code == 400
        assert "Embedding 生成失败" in resp.json()["message"]


# ============================================================
# POST /evidence/embed-batch —— 批量 embedding
# ============================================================
class TestEmbedBatch:
    async def test_batch_skipped_when_llm_not_configured(self, evidence_client, evidence_env):
        with patch("shared.llm_client.llm_client", _mock_llm(supports=False)):
            resp = await evidence_client.post("/api/v1/evidence/embed-batch")

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "skipped"
        assert data["embedded_count"] == 0
        assert "未配置" in data["message"]

    async def test_batch_success_with_case_filter(self, evidence_client, test_db, evidence_env):
        llm = _mock_llm(embed_batch=[[0.1, 0.2], [0.3, 0.4]])
        with patch("shared.llm_client.llm_client", llm):
            resp = await evidence_client.post(
                "/api/v1/evidence/embed-batch", params={"case_id": "DEV-LIST-A", "limit": 10}
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "ok"
        assert data["embedded_count"] == 2  # DEV-LIST-A 的两条证据
        assert data["embedding_model"] == "embedding-3"
        llm.embed_texts.assert_awaited_once()

        # 审计：EVIDENCE_EMBED_BATCH 已写入
        logs = (
            await test_db.execute(
                select(AuditLog).where(AuditLog.action == "EVIDENCE_EMBED_BATCH")
            )
        ).scalars().all()
        assert len(logs) == 1
        assert logs[0].target == "DEV-LIST-A"

    async def test_batch_success_without_case_filter(self, evidence_client, evidence_env):
        llm = _mock_llm(embed_batch=[[0.1], [0.2], [0.3]])
        with patch("shared.llm_client.llm_client", llm):
            resp = await evidence_client.post("/api/v1/evidence/embed-batch")

        assert resp.status_code == 200
        assert resp.json()["embedded_count"] == 3  # 全部证据

    async def test_batch_no_pending_evidence(self, evidence_client, evidence_env, test_db):
        # 全部证据已用当前模型嵌入 → 无待嵌入
        for eid in ("E-LIST-01", "E-LIST-02", "E-LIST-03"):
            ev = await test_db.get(Evidence, eid)
            ev.embedding = [0.5]
            ev.embedding_model = "embedding-3"
            ev.embedded_at = datetime.now(UTC)
        await test_db.commit()

        llm = _mock_llm()
        with patch("shared.llm_client.llm_client", llm):
            resp = await evidence_client.post("/api/v1/evidence/embed-batch")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["embedded_count"] == 0
        assert "无待嵌入证据" in data["message"]
        llm.embed_texts.assert_not_awaited()

    async def test_batch_reembeds_when_model_changed(self, evidence_client, test_db, evidence_env):
        # E-LIST-01 用旧模型嵌入 → 需重嵌；其余为当前模型 → 跳过
        ev_old = await test_db.get(Evidence, "E-LIST-01")
        ev_old.embedding = [0.9]
        ev_old.embedding_model = "embedding-v2"
        ev_old.embedded_at = datetime.now(UTC)
        for eid in ("E-LIST-02", "E-LIST-03"):
            ev = await test_db.get(Evidence, eid)
            ev.embedding = [0.5]
            ev.embedding_model = "embedding-3"
            ev.embedded_at = datetime.now(UTC)
        await test_db.commit()

        llm = _mock_llm(embed_batch=[[0.1, 0.2]])
        with patch("shared.llm_client.llm_client", llm):
            resp = await evidence_client.post("/api/v1/evidence/embed-batch")

        assert resp.status_code == 200
        assert resp.json()["embedded_count"] == 1

        await test_db.refresh(ev_old)
        assert ev_old.embedding_model == "embedding-3"
        assert ev_old.embedding == [0.1, 0.2]

    async def test_batch_llm_error_returns_400(self, evidence_client, evidence_env):
        llm = _mock_llm(error=LLMError("quota exceeded"))
        with patch("shared.llm_client.llm_client", llm):
            resp = await evidence_client.post("/api/v1/evidence/embed-batch")

        assert resp.status_code == 400
        assert "批量嵌入失败" in resp.json()["message"]
        assert "第 1 批" in resp.json()["message"]


# ============================================================
# semantic_search（直接调用 —— HTTP 层被 /evidence/{id} 遮蔽）
# ============================================================
class TestSemanticSearchEndpoint:
    async def test_keyword_mode_when_llm_unconfigured(self, test_db, evidence_env):
        with patch("shared.llm_client.llm_client", _mock_llm(supports=False)):
            result = await semantic_search(
                request=MagicMock(),
                q="差压",
                top_k=10,
                exclude_case_id=None,
                db=test_db,
                _=MagicMock(),
            )

        assert result["query"] == "差压"
        assert result["search_mode"] == "keyword"
        assert result["embedding_error"] is None
        assert result["result_count"] >= 1
        assert any(r["evidence_id"] == "E-LIST-02" for r in result["results"])
        assert all(r["matched_by"] == "keyword" for r in result["results"])

    async def test_semantic_mode_with_embedding(self, test_db, evidence_env):
        ev = await test_db.get(Evidence, "E-LIST-02")
        ev.embedding = [1.0, 0.0]
        await test_db.commit()

        llm = _mock_llm(embed=[1.0, 0.0])
        with patch("shared.llm_client.llm_client", llm):
            result = await semantic_search(
                request=MagicMock(),
                q="AHU",
                top_k=10,
                exclude_case_id=None,
                db=test_db,
                _=MagicMock(),
            )

        assert result["search_mode"] == "semantic"
        assert result["embedding_error"] is None
        top = result["results"][0]
        assert top["evidence_id"] == "E-LIST-02"
        assert top["matched_by"] == "semantic"
        assert top["score"] == pytest.approx(1.0)

    async def test_llm_error_falls_back_to_keyword(self, test_db, evidence_env):
        llm = _mock_llm(error=LLMError("net down"))
        with patch("shared.llm_client.llm_client", llm):
            result = await semantic_search(
                request=MagicMock(),
                q="差压",
                top_k=10,
                exclude_case_id=None,
                db=test_db,
                _=MagicMock(),
            )

        assert result["search_mode"] == "keyword"
        assert result["embedding_error"] == "net down"
        assert result["result_count"] >= 1

    async def test_exclude_case_id_filters_results(self, test_db, evidence_env):
        with patch("shared.llm_client.llm_client", _mock_llm(supports=False)):
            result = await semantic_search(
                request=MagicMock(),
                q="沉降菌",
                top_k=10,
                exclude_case_id="DEV-LIST-A",
                db=test_db,
                _=MagicMock(),
            )

        assert result["exclude_case_id"] == "DEV-LIST-A"
        assert result["result_count"] >= 1
        assert all(r["case_id"] != "DEV-LIST-A" for r in result["results"])
        assert any(r["evidence_id"] == "E-LIST-03" for r in result["results"])


# ============================================================
# find_related_cases（直接调用 —— HTTP 层被 /evidence/{id} 遮蔽）
# ============================================================
class TestFindRelatedCasesEndpoint:
    async def test_related_cases_keyword_mode(self, test_db, evidence_env):
        with patch("shared.llm_client.llm_client", _mock_llm(supports=False)):
            result = await find_related_cases(
                request=MagicMock(),
                q="沉降菌超标",
                top_k=5,
                exclude_case_id="DEV-LIST-A",
                db=test_db,
                _=MagicMock(),
            )

        assert result["exclude_case_id"] == "DEV-LIST-A"
        assert result["embedding_error"] is None
        assert result["related_count"] == len(result["related_cases"])
        case_ids = [r["case_id"] for r in result["related_cases"]]
        assert "DEV-LIST-B" in case_ids
        assert "DEV-LIST-A" not in case_ids

    async def test_related_cases_semantic_mode(self, test_db, evidence_env):
        ev = await test_db.get(Evidence, "E-LIST-03")
        ev.embedding = [1.0, 0.0]
        await test_db.commit()

        llm = _mock_llm(embed=[1.0, 0.0])
        with patch("shared.llm_client.llm_client", llm):
            result = await find_related_cases(
                request=MagicMock(),
                q="环境监测",
                top_k=5,
                exclude_case_id="DEV-LIST-A",
                db=test_db,
                _=MagicMock(),
            )

        matched = [r for r in result["related_cases"] if r["case_id"] == "DEV-LIST-B"]
        assert matched
        assert matched[0]["matched_by"] == "semantic"
        assert matched[0]["evidence_count"] >= 1

    async def test_related_cases_llm_error_fallback(self, test_db, evidence_env):
        llm = _mock_llm(error=LLMError("timeout"))
        with patch("shared.llm_client.llm_client", llm):
            result = await find_related_cases(
                request=MagicMock(),
                q="沉降菌",
                top_k=5,
                exclude_case_id=None,
                db=test_db,
                _=MagicMock(),
            )

        assert result["embedding_error"] == "timeout"
        assert result["related_count"] >= 1
        case_ids = [r["case_id"] for r in result["related_cases"]]
        assert "DEV-LIST-B" in case_ids


# ============================================================
# 直接调用 —— HTTP 层测试的函数级补充
#
# 背景：Python 3.12 + coverage 7.0 的 sys.settrace 在协程 await 恢复后
# 存在 line 事件丢失问题（经 ASGI/中间件调用链时尤为明显），导致部分
# 确实执行过的行（如 return / 赋值）未被记录。直接调用端点函数可完整
# 跟踪 —— 与 tests/test_evidence_semantic.py 直接调用 semantic_search
# 的做法一致。HTTP 层行为已由上面的 ASGI 测试验证。
# ============================================================
class TestDirectCallEndpoints:
    async def test_list_evidence_no_filter(self, test_db, evidence_env):
        result = await list_evidence(
            request=MagicMock(),
            case_id=None,
            source_system=None,
            min_confidence=None,
            keyword=None,
            page=1,
            page_size=50,
            db=test_db,
            _=MagicMock(),
        )
        assert result.total == 3
        assert [it.id for it in result.items] == ["E-LIST-01", "E-LIST-02", "E-LIST-03"]

    async def test_list_evidence_pagination(self, test_db, evidence_env):
        result = await list_evidence(
            request=MagicMock(),
            case_id=None,
            source_system=None,
            min_confidence=None,
            keyword=None,
            page=2,
            page_size=2,
            db=test_db,
            _=MagicMock(),
        )
        assert result.total == 3
        assert result.page == 2
        assert [it.id for it in result.items] == ["E-LIST-03"]

    async def test_get_evidence_found(self, test_db, evidence_env):
        result = await get_evidence(
            request=MagicMock(), evidence_id="E-LIST-01", db=test_db, _=MagicMock()
        )
        assert result.id == "E-LIST-01"
        assert result.raw_data == {"employee_id": "EMP-2187"}

    async def test_get_evidence_missing_raises(self, test_db, seed_db):
        with pytest.raises(NotFoundError, match="不存在"):
            await get_evidence(
                request=MagicMock(), evidence_id="E-NOPE", db=test_db, _=MagicMock()
            )

    async def test_get_timeline(self, test_db, evidence_env):
        result = await get_timeline(
            request=MagicMock(), case_id="DEV-LIST-A", db=test_db, _=MagicMock()
        )
        assert [e.id for e in result] == ["T-EV-02", "T-EV-01", "T-EV-03"]


@pytest_asyncio.fixture
async def direct_auth(test_db: AsyncSession, seed_db) -> AuthContext:
    """直接调用端点函数用的 AuthContext（investigator —— evidence:read/write）"""
    user = User(
        id="U-DIRECT",
        email=LISA_EMAIL,
        name="Direct Caller",
        role="investigator",
        is_active=True,
    )
    return AuthContext(
        user=user,
        session_id="direct-session",
        ip_address="127.0.0.1",
        permissions=ROLE_PERMISSIONS.get("investigator", set()),
        db=test_db,
    )


class TestDirectCallFetchFromSource:
    async def test_fetch_bms_persist_and_dedup(self, test_db, seed_db, direct_auth):
        payload = {"items": [{"ahu_id": "AHU-7", "summary": "AHU-7 差压 470 Pa"}]}
        mock_client = _connector_http_client(payload)
        with patch("evidence_service.routes.httpx.AsyncClient", return_value=mock_client):
            r1 = await fetch_from_source(
                request=MagicMock(),
                source="BMS",
                case_id="DEV-DIRECT",
                db=test_db,
                auth=direct_auth,
            )
            r2 = await fetch_from_source(
                request=MagicMock(),
                source="BMS",
                case_id="DEV-DIRECT",
                db=test_db,
                auth=direct_auth,
            )

        assert r1["status"] == "ok"
        assert r1["source"] == "BMS"
        assert r1["case_id"] == "DEV-DIRECT"
        assert r1["fetched_count"] == 1
        assert r1["persisted_count"] == 1
        assert r1["triggered_by"] == LISA_EMAIL
        # 第二次拉取同 payload —— content_hash 命中已有记录 → 跳过（幂等）
        assert r2["fetched_count"] == 1
        assert r2["persisted_count"] == 0

        # 落库验证（append_audit_log 内部 flush，Evidence 已可见）
        rows = (
            await test_db.execute(select(Evidence).where(Evidence.case_id == "DEV-DIRECT"))
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].source_system == "BMS"
        assert rows[0].summary == "[BMS] AHU-7 差压 470 Pa"

        # 审计：两次拉取各写一条 EVIDENCE_FETCHED
        logs = (
            await test_db.execute(select(AuditLog).where(AuditLog.action == "EVIDENCE_FETCHED"))
        ).scalars().all()
        assert len(logs) == 2
        assert all(log.operator == LISA_EMAIL for log in logs)

    async def test_fetch_lms_derives_employee_id_param(self, test_db, seed_db, direct_auth):
        test_db.add(_lms_evidence("E-LMS-DIRECT", "DEV-LMS-DIRECT", {"employee_id": "EMP-2187"}))
        await test_db.commit()

        mock_client = _connector_http_client({"employees": [{"course_name": "无菌更衣"}]})
        with patch("evidence_service.routes.httpx.AsyncClient", return_value=mock_client):
            result = await fetch_from_source(
                request=MagicMock(),
                source="LMS",
                case_id="DEV-LMS-DIRECT",
                db=test_db,
                auth=direct_auth,
            )

        assert result["status"] == "ok"
        assert result["persisted_count"] == 1
        # 从已有 LMS 证据推导员工号并作为查询参数传给 Connector
        assert mock_client.get.call_args.kwargs["params"].get("employee_id") == "EMP-2187"
        assert "/lms/cert-status" in mock_client.get.call_args.args[0]


class TestDirectCallEmbedEvidence:
    async def test_embed_missing_raises_404(self, test_db, seed_db, direct_auth):
        with pytest.raises(NotFoundError, match="不存在"):
            await embed_evidence(
                request=MagicMock(), evidence_id="E-NOPE", db=test_db, auth=direct_auth
            )

    async def test_embed_no_summary_raises_400(self, test_db, seed_db, direct_auth):
        test_db.add(
            Evidence(
                id="E-NOSUM-D",
                case_id="DEV-NOSUM-D",
                source_system="LIMS",
                collected_at=datetime.now(UTC),
                summary=None,
                raw_data={},
                content_hash="sha256:nosum-d",
                confidence=3,
            )
        )
        await test_db.commit()

        with pytest.raises(ValidationError, match="无摘要"):
            await embed_evidence(
                request=MagicMock(), evidence_id="E-NOSUM-D", db=test_db, auth=direct_auth
            )

    async def test_embed_skipped_when_llm_unconfigured(self, test_db, evidence_env, direct_auth):
        with patch("shared.llm_client.llm_client", _mock_llm(supports=False)):
            result = await embed_evidence(
                request=MagicMock(), evidence_id="E-LIST-01", db=test_db, auth=direct_auth
            )
        assert result["status"] == "skipped"
        assert result["embedding_model"] is None
        assert "未配置" in result["message"]

    async def test_embed_success_persists_and_audits(self, test_db, evidence_env, direct_auth):
        llm = _mock_llm(embed=[0.4, 0.5, 0.6])
        with patch("shared.llm_client.llm_client", llm):
            result = await embed_evidence(
                request=MagicMock(), evidence_id="E-LIST-01", db=test_db, auth=direct_auth
            )

        assert result["status"] == "ok"
        assert result["embedding_dim"] == 3
        assert result["embedding_model"] == "embedding-3"
        assert "embedded_at" in result
        llm.embed_text.assert_awaited_once_with("EMP-2187 更衣资质逾期")

        ev = await test_db.get(Evidence, "E-LIST-01")
        await test_db.refresh(ev)
        assert ev.embedding == [0.4, 0.5, 0.6]
        assert ev.embedding_model == "embedding-3"
        assert ev.embedded_at is not None

        logs = (
            await test_db.execute(
                select(AuditLog).where(AuditLog.action == "EVIDENCE_EMBEDDED")
            )
        ).scalars().all()
        assert len(logs) == 1
        assert logs[0].operator == LISA_EMAIL

    async def test_embed_llm_error_raises_400(self, test_db, evidence_env, direct_auth):
        llm = _mock_llm(error=LLMError("provider down"))
        with patch("shared.llm_client.llm_client", llm):
            with pytest.raises(ValidationError, match="Embedding 生成失败"):
                await embed_evidence(
                    request=MagicMock(), evidence_id="E-LIST-01", db=test_db, auth=direct_auth
                )


class TestDirectCallEmbedBatch:
    async def test_batch_no_pending_evidence(self, test_db, evidence_env, direct_auth):
        for eid in ("E-LIST-01", "E-LIST-02", "E-LIST-03"):
            ev = await test_db.get(Evidence, eid)
            ev.embedding = [0.5]
            ev.embedding_model = "embedding-3"
            ev.embedded_at = datetime.now(UTC)
        await test_db.commit()

        llm = _mock_llm()
        with patch("shared.llm_client.llm_client", llm):
            result = await embed_evidence_batch(
                request=MagicMock(), case_id=None, limit=100, db=test_db, auth=direct_auth
            )

        assert result["status"] == "ok"
        assert result["embedded_count"] == 0
        assert "无待嵌入证据" in result["message"]
        llm.embed_texts.assert_not_awaited()

    async def test_batch_success_with_case_filter(self, test_db, evidence_env, direct_auth):
        llm = _mock_llm(embed_batch=[[0.1, 0.2], [0.3, 0.4]])
        with patch("shared.llm_client.llm_client", llm):
            result = await embed_evidence_batch(
                request=MagicMock(), case_id="DEV-LIST-A", limit=100, db=test_db, auth=direct_auth
            )

        assert result["status"] == "ok"
        assert result["embedded_count"] == 2
        assert result["embedding_model"] == "embedding-3"
        llm.embed_texts.assert_awaited_once()

        ev = await test_db.get(Evidence, "E-LIST-01")
        await test_db.refresh(ev)
        assert ev.embedding == [0.1, 0.2]
        assert ev.embedding_model == "embedding-3"

        logs = (
            await test_db.execute(
                select(AuditLog).where(AuditLog.action == "EVIDENCE_EMBED_BATCH")
            )
        ).scalars().all()
        assert len(logs) == 1
        assert logs[0].target == "DEV-LIST-A"

    async def test_batch_llm_error_raises_400(self, test_db, evidence_env, direct_auth):
        llm = _mock_llm(error=LLMError("quota exceeded"))
        with patch("shared.llm_client.llm_client", llm):
            with pytest.raises(ValidationError, match="批量嵌入失败"):
                await embed_evidence_batch(
                    request=MagicMock(), case_id=None, limit=100, db=test_db, auth=direct_auth
                )
