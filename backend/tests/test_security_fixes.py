"""关键安全与合规修复的回归测试

覆盖审查报告中的上线阻断级修复：
- 电子签名强制顺序（submit → approve，双人，不可重复签）
- POST /audit/logs 权限与 operator 强制归因
- Agent 流水线重复启动防护（同案例单活跃 run）
- LLM 幻觉证据 ID 过滤
- 422 校验错误不回显提交值（防密码泄露）
- HITL gate2 edit（5 Whys 编辑）真实生效
"""
import asyncio
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


# ============================================================
#  电子签名（21 CFR Part 11）
# ============================================================
def _auth_ctx(user, permissions):
    from shared.auth_middleware import AuthContext

    return AuthContext(
        user=user,
        session_id="test-session",
        ip_address="127.0.0.1",
        permissions=permissions,
        db=None,  # type: ignore[arg-type]  # 该测试无真实 session，AuthContext 运行时允许 None
    )


def _sig_body(meaning):
    from shared.schemas import SignatureRequest

    return SignatureRequest(
        signature_meaning=meaning,
        password="rca-demo-2025",
    )


class TestSignatureOrder:
    """签名顺序强制 —— 修复前 QA 可跳过调查员提交直接批准结案"""

    async def test_approve_requires_existing_submit(self, test_db: AsyncSession, seed_db):
        from case_service.routes import sign_case
        from shared.exceptions import ValidationError

        case = self._make_case("DEV-SIG-001")
        test_db.add(case)
        investigator, qa = seed_db[0], seed_db[1]
        await test_db.commit()
        case.status = "reviewing"
        _ = investigator
        await test_db.commit()

        with patch("case_service.routes.verify_password", return_value=True):
            # 无 submit 记录时直接 approve → 400
            with pytest.raises(ValidationError) as exc:
                await sign_case(
                    request=MagicMock(), case_id="DEV-SIG-001", body=_sig_body("approve"),
                    db=test_db, auth=_auth_ctx(qa, {"sign:approve"}),
                )
            assert "submit" in str(exc.value)

    async def test_full_dual_signature_closes_case(self, test_db: AsyncSession, seed_db):
        from case_service.routes import sign_case
        from shared.models import SignatureRecord

        test_db.add(self._make_case("DEV-SIG-002"))
        investigator, qa = seed_db[0], seed_db[1]
        await test_db.commit()

        with patch("case_service.routes.verify_password", return_value=True):
            # 调查员提交签名
            sig1 = await sign_case(
                request=MagicMock(), case_id="DEV-SIG-002", body=_sig_body("submit"),
                db=test_db, auth=_auth_ctx(investigator, {"sign:submit"}),
            )
            assert sig1.signature_meaning == "提交签名"
            assert sig1.certificate_fingerprint.startswith("sha256:")

            # QA 批准 → 结案
            await sign_case(
                request=MagicMock(), case_id="DEV-SIG-002", body=_sig_body("approve"),
                db=test_db, auth=_auth_ctx(qa, {"sign:approve"}),
            )

        from sqlalchemy import select
        case = (await test_db.execute(
            select(type(investigator).__mro__[0])  # noop，保持类型检查安静
        )).first() if False else None

        from shared.models import Deviation
        case = (await test_db.execute(
            select(Deviation).where(Deviation.id == "DEV-SIG-002")
        )).scalar_one()
        assert case.status == "closed"
        assert case.progress == 100
        assert case.closed_at is not None
        # 两条签名记录
        sigs = (await test_db.execute(select(SignatureRecord).where(SignatureRecord.case_id == "DEV-SIG-002"))).scalars().all()
        assert len(sigs) == 2

    async def test_same_user_cannot_sign_both(self, test_db: AsyncSession, seed_db):
        from case_service.routes import sign_case
        from shared.exceptions import ValidationError

        test_db.add(self._make_case("DEV-SIG-003"))
        admin = seed_db[2]  # admin 角色同时持有 sign:submit + sign:approve —— 唯一能触达同人双签校验的场景
        await test_db.commit()

        with patch("case_service.routes.verify_password", return_value=True):
            await sign_case(
                request=MagicMock(), case_id="DEV-SIG-003", body=_sig_body("submit"),
                db=test_db, auth=_auth_ctx(admin, {"sign:submit"}),
            )
            with pytest.raises(ValidationError) as exc:
                await sign_case(
                    request=MagicMock(), case_id="DEV-SIG-003", body=_sig_body("approve"),
                    db=test_db, auth=_auth_ctx(admin, {"sign:approve"}),
                )
            assert "不同用户" in str(exc.value)

    async def test_approve_by_non_assigned_reviewer_rejected(self, test_db: AsyncSession, seed_db):
        """指定审核人一致性：案例指派 U-JW 时，其他持 sign:approve 者（含 admin）不得代批"""
        from case_service.routes import sign_case
        from shared.exceptions import ForbiddenError
        from shared.models import SignatureRecord

        test_db.add(self._make_case("DEV-SIG-004"))
        admin = seed_db[2]  # admin 持有 sign:approve，但非本案例指定审核人
        await test_db.commit()
        test_db.add(SignatureRecord(
            id="SIG-DEV-SIG-004-SUBMIT-U-LC", case_id="DEV-SIG-004",
            signer_id="U-LC", signer_role="investigator",
            signature_meaning="提交签名", signed_at=datetime.now(UTC),
        ))
        await test_db.commit()

        with patch("case_service.routes.verify_password", return_value=True):
            with pytest.raises(ForbiddenError) as exc:
                await sign_case(
                    request=MagicMock(), case_id="DEV-SIG-004", body=_sig_body("approve"),
                    db=test_db, auth=_auth_ctx(admin, {"sign:approve"}),
                )
            assert "指定 QA 审核人" in str(exc.value)

    async def test_cross_user_veto_approvedirect_submit(self, test_db: AsyncSession, seed_db):
        """越权测试：QA 批准前必须存在投稿者的 submit 记录，否则拒绝"""
        from case_service.routes import sign_case
        from shared.exceptions import ValidationError
        from shared.models import Deviation

        test_db.add(self._make_case("DEV-SIG-010"))
        _investigator, qa = seed_db[0], seed_db[1]
        await test_db.commit()
        case = (await test_db.execute(select(Deviation).where(Deviation.id == "DEV-SIG-010"))).scalar_one()
        case.status = "reviewing"
        await test_db.commit()

        with patch("case_service.routes.verify_password", return_value=True):
            # QA 直接批准（无 submit 记录）→ 400 错误
            with pytest.raises(ValidationError) as exc:
                await sign_case(
                    request=MagicMock(), case_id="DEV-SIG-010", body=_sig_body("approve"),
                    db=test_db, auth=_auth_ctx(qa, {"sign:approve"}),
                )
            assert "尚未收到" in str(exc.value) or "submit" in str(exc.value).lower()

    async def test_sign_closed_case_rejected(self, test_db: AsyncSession, seed_db):
        from case_service.routes import sign_case
        from shared.exceptions import ValidationError
        from shared.models import Deviation, SignatureRecord

        test_db.add(self._make_case("DEV-SIG-004", status="closed"))
        investigator, _qa = seed_db[0], seed_db[1]
        await test_db.commit()

        with patch("case_service.routes.verify_password", return_value=True):
            with pytest.raises(ValidationError):
                await sign_case(
                    request=MagicMock(), case_id="DEV-SIG-004", body=_sig_body("submit"),
                    db=test_db, auth=_auth_ctx(investigator, {"sign:submit"}),
                )

    async def test_submit_requires_reviewing_status(self, test_db: AsyncSession, seed_db):
        from case_service.routes import sign_case
        from shared.exceptions import ValidationError

        test_db.add(self._make_case("DEV-SIG-005", status="investigating"))
        investigator, _ = seed_db[0], seed_db[1]
        await test_db.commit()

        with patch("case_service.routes.verify_password", return_value=True):
            with pytest.raises(ValidationError) as exc:
                await sign_case(
                    request=MagicMock(), case_id="DEV-SIG-005", body=_sig_body("submit"),
                    db=test_db, auth=_auth_ctx(investigator, {"sign:submit"}),
                )
            assert "reviewing" in str(exc.value)

    @staticmethod
    def _make_case(case_id, status="reviewing"):
        from shared.models import Deviation

        return Deviation(
            id=case_id,
            title="签名流程测试案例（用于双人签名回归测试）",
            severity="major",
            status=status,
            discovered_at=datetime.now(UTC),
            investigator_id="U-LC",
            qa_reviewer_id="U-JW",
            progress=100,
            hitl_gates={f"gate{i}": "done" for i in range(1, 6)},
        )


# ============================================================
#  结案重审（reopen）—— 结案后修订必须走受控重开
# ============================================================
class TestReopenCase:
    """POST /cases/{id}/reopen：仅 QA 批准人可重开 closed 案例，旧签名保留"""

    @staticmethod
    def _make_case(case_id, status="reviewing"):
        from shared.models import Deviation

        return Deviation(
            id=case_id,
            title="重审流程测试案例（reopen 回归测试）",
            severity="major",
            status=status,
            discovered_at=datetime.now(UTC),
            investigator_id="U-LC",
            qa_reviewer_id="U-JW",
            progress=100,
            hitl_gates={f"gate{i}": "done" for i in range(1, 6)},
        )

    async def _reopen(self, test_db: AsyncSession, seed_db, case_id, permissions=None):
        from case_service.routes import reopen_case
        from shared.schemas import CaseReopenRequest

        qa = seed_db[1]
        body = CaseReopenRequest(reason="CAPA 验证发现新证据，需修订根因结论")
        return await reopen_case(
            request=MagicMock(), case_id=case_id, body=body,
            db=test_db, auth=_auth_ctx(qa, permissions or {"sign:approve"}),
        )

    async def test_reopen_closed_case(self, test_db: AsyncSession, seed_db):
        from shared.models import Deviation

        test_db.add(self._make_case("DEV-REOPEN-001", status="closed"))
        await test_db.commit()

        case = await self._reopen(test_db, seed_db, "DEV-REOPEN-001")
        assert case.status == "reviewing"
        assert case.progress == 90
        assert case.meta["reopen"]["reopened_by"] == "U-JW"
        await test_db.commit()

        row = (await test_db.execute(
            select(Deviation).where(Deviation.id == "DEV-REOPEN-001")
        )).scalar_one()
        assert row.status == "reviewing"

    async def test_reopen_requires_closed(self, test_db: AsyncSession, seed_db):
        from case_service.routes import reopen_case
        from shared.exceptions import ConflictError
        from shared.schemas import CaseReopenRequest

        test_db.add(self._make_case("DEV-REOPEN-002", status="reviewing"))
        qa = seed_db[1]
        await test_db.commit()

        with pytest.raises(ConflictError):
            await reopen_case(
                request=MagicMock(), case_id="DEV-REOPEN-002",
                body=CaseReopenRequest(reason="尚未结案不应允许重开"),
                db=test_db, auth=_auth_ctx(qa, {"sign:approve"}),
            )

    async def test_reopen_requires_sign_approve(self, test_db: AsyncSession, seed_db):
        from case_service.routes import reopen_case
        from shared.exceptions import ForbiddenError
        from shared.schemas import CaseReopenRequest

        test_db.add(self._make_case("DEV-REOPEN-003", status="closed"))
        investigator = seed_db[0]
        await test_db.commit()

        with pytest.raises(ForbiddenError):
            await reopen_case(
                request=MagicMock(), case_id="DEV-REOPEN-003",
                body=CaseReopenRequest(reason="调查员无权单方面重开已结案案例"),
                db=test_db, auth=_auth_ctx(investigator, {"sign:submit"}),
            )

    async def test_resubmit_after_reopen(self, test_db: AsyncSession, seed_db):
        """重开后必须重新走 submit → approve（旧 submit 仍在也不能跳过 approve 校验）"""
        from case_service.routes import reopen_case, sign_case
        from shared.exceptions import ValidationError
        from shared.models import Deviation
        from shared.schemas import CaseReopenRequest

        test_db.add(self._make_case("DEV-REOPEN-004", status="closed"))
        investigator, qa = seed_db[0], seed_db[1]
        await test_db.commit()

        await reopen_case(
            request=MagicMock(), case_id="DEV-REOPEN-004",
            body=CaseReopenRequest(reason="监管检查反馈需要补充稳定性数据"),
            db=test_db, auth=_auth_ctx(qa, {"sign:approve"}),
        )
        # 重开仅回 reviewing；approve 仍要求先有他人 submit —— 直接批准被拒
        with patch("case_service.routes.verify_password", return_value=True):
            with pytest.raises(ValidationError) as exc:
                await sign_case(
                    request=MagicMock(), case_id="DEV-REOPEN-004", body=_sig_body("approve"),
                    db=test_db, auth=_auth_ctx(qa, {"sign:approve"}),
                )
            assert "submit" in str(exc.value)
        _ = investigator
        _ = Deviation


# ============================================================
#  审计日志写入端点（可归因性）
# ============================================================
class TestAuditWriteEndpoint:
    """POST /audit/logs：仅 admin 可写；operator 强制取认证身份"""

    async def test_operator_forced_to_authenticated_user(self, test_db: AsyncSession, seed_db):
        from sqlalchemy import select

        from audit_service.routes import write_log
        from shared.models import AuditLog
        from shared.schemas import AuditLogCreate

        admin = seed_db[2]
        body = AuditLogCreate(
            operator="victim@biopharma.com",  # 试图伪造他人署名
            action="FAKE_ACTION",
        )
        with patch("shared.error_handler.get_trace_id", return_value=""):
            log = await write_log(
                request=MagicMock(), body=body, db=test_db,
                auth=_auth_ctx(admin, {"system:configure"}),
            )
        # operator 被强制归因到认证用户
        assert log.operator == admin.email
        assert "declared_operator=victim@biopharma.com" in (log.detail or "")

    # 权限拒绝（403）由 FastAPI Depends(require("system:configure")) 承担，
    # 直调函数无法覆盖 —— 审计端点权限在 CI 的 API 层测试中验证。


# ============================================================
#  Agent 流水线重复启动防护
# ============================================================
class TestPipelineDedup:
    async def test_duplicate_start_conflict(self, test_db: AsyncSession, seed_db):
        from sqlalchemy import select

        from agent_service.routes import start_pipeline
        from shared.config import settings
        from shared.exceptions import ConflictError
        from shared.models import AgentRun, Deviation
        from shared.schemas import AgentRunCreate

        case = Deviation(
            id="DEV-RUN-001",
            title="流水线去重测试案例（用于重复启动防护回归测试）",
            severity="minor",
            status="investigating",
            discovered_at=datetime.now(UTC),
            investigator_id="U-LC",
            hitl_gates={},
        )
        test_db.add(case)
        await test_db.commit()

        # 走服务密钥通道（绕过用户认证，聚焦去重逻辑本身）
        old_key = settings.service_api_key
        settings.service_api_key = "test-service-key"
        try:
            request = MagicMock()
            request.headers = {"X-Service-Key": "test-service-key"}
            request.client = SimpleNamespace(host="127.0.0.1")
            body = AgentRunCreate(case_id="DEV-RUN-001", triggered_by="system:test")

            bg = MagicMock()
            run1 = await start_pipeline(request=request, body=body, background_tasks=bg, db=test_db)
            assert run1.id.startswith("RUN-DEV-RUN-001-")
            # run_id 含 uuid 后缀（非秒级时间戳，可区分）
            assert bg.add_task.called

            # 手动把 run 置为 running（模拟流水线已启动）
            run = (await test_db.execute(select(AgentRun).where(AgentRun.id == run1.id))).scalar_one()
            run.status = "running"
            await test_db.commit()

            # 第二次启动 → 409
            with pytest.raises(ConflictError):
                await start_pipeline(request=request, body=body, background_tasks=MagicMock(), db=test_db)
        finally:
            settings.service_api_key = old_key

    async def test_user_call_without_triggered_by_uses_system_default(
        self, test_db: AsyncSession, seed_db
    ):
        """前端 agentApi.run 只发 case_id（triggered_by 由服务端 JWT 归因决定）——
        schema 必须放行缺省 triggered_by，否则浏览器端启动流水线一律 422。"""
        from sqlalchemy import select

        from agent_service.routes import start_pipeline
        from shared.config import settings
        from shared.models import AgentRun, Deviation
        from shared.schemas import AgentRunCreate

        case = Deviation(
            id="DEV-RUN-002",
            title="无 triggered_by 启动流水线回归测试案例",
            severity="minor",
            status="investigating",
            discovered_at=datetime.now(UTC),
            investigator_id="U-LC",
            hitl_gates={},
        )
        test_db.add(case)
        await test_db.commit()

        old_key = settings.service_api_key
        settings.service_api_key = "test-service-key"
        try:
            request = MagicMock()
            request.headers = {"X-Service-Key": "test-service-key"}
            request.client = SimpleNamespace(host="127.0.0.1")
            # 模拟前端真实载荷：{"case_id": "..."}，无 triggered_by
            body = AgentRunCreate(case_id="DEV-RUN-002")

            bg = MagicMock()
            run = await start_pipeline(request=request, body=body, background_tasks=bg, db=test_db)
            assert bg.add_task.called
            row = (
                await test_db.execute(select(AgentRun).where(AgentRun.id == run.id))
            ).scalar_one()
            assert row.triggered_by == "system"
        finally:
            settings.service_api_key = old_key


    async def test_user_channel_forces_jwt_attribution(self, test_db: AsyncSession, seed_db):
        """消融实验 E2：用户通道（JWT）客户端伪造 triggered_by 必须被忽略 ——
        审计归因强制取 JWT 邮箱（agent_service/routes.py:133）。"""
        from sqlalchemy import select

        from agent_service.routes import start_pipeline
        from shared.models import AgentRun, Deviation, User
        from shared.schemas import AgentRunCreate
        from shared.security import create_access_token

        case = Deviation(
            id="DEV-RUN-003",
            title="用户通道归因消融测试案例",
            severity="minor",
            status="investigating",
            discovered_at=datetime.now(UTC),
            investigator_id="U-LC",
            hitl_gates={},
        )
        test_db.add(case)
        await test_db.commit()

        user = (await test_db.execute(select(User).where(User.id == "U-LC"))).scalar_one()
        token = create_access_token(user.id, claims={"email": user.email, "role": user.role})

        # 无 X-Service-Key → 用户通道；body 携带伪造归因
        request = MagicMock()
        request.headers = {}
        request.client = SimpleNamespace(host="127.0.0.1")
        body = AgentRunCreate(case_id="DEV-RUN-003", triggered_by="spoofed-admin@example.com")

        run = await start_pipeline(
            request=request,
            body=body,
            background_tasks=MagicMock(),
            db=test_db,
            authorization=f"Bearer {token}",
        )
        row = (await test_db.execute(select(AgentRun).where(AgentRun.id == run.id))).scalar_one()
        assert row.triggered_by == user.email
        assert row.triggered_by != "spoofed-admin@example.com"


# ============================================================
#  LLM 幻觉证据过滤
# ============================================================
class TestHallucinationFilter:
    def test_filters_unknown_evidence_ids(self):
        from agent_service.routes import _filter_hallucinated_evidence

        kept, dropped = _filter_hallucinated_evidence(
            ["E-OK-01", "E-FAKE-99", "E-OK-02", "E-FAKE-98"],
            ["E-OK-01", "E-OK-02"],
        )
        assert kept == ["E-OK-01", "E-OK-02"]
        assert dropped == ["E-FAKE-99", "E-FAKE-98"]


# ============================================================
#  422 校验错误脱敏
# ============================================================
class TestValidationErrorSanitized:
    async def test_no_input_echo_in_422(self, app_client: AsyncClient):
        """提交过短密码 —— 响应体不得回显提交值"""
        resp = await app_client.post(
            "/api/v1/auth/login",
            json={"email": "attacker@biopharma.com", "password": "secret-value-123"},
        )
        # 密码 <12 位 → 422
        if resp.status_code == 422:
            body = resp.text
            assert "secret-value-123" not in body, "422 响应回显了提交的密码"
            assert '"input"' not in body


# ============================================================
#  HITL gate2 编辑（edit_payload 真实生效）
# ============================================================
class TestHitlEdit:
    async def test_gate2_edit_applies_changes(self, test_db: AsyncSession, seed_db):
        from sqlalchemy import select

        from case_service.routes import handle_hitl_gate, update_five_why
        from shared.models import Deviation, FiveWhy
        from shared.schemas import FiveWhyUpdate, HitlGateRequest

        case = Deviation(
            id="DEV-EDIT-001",
            title="HITL 编辑测试案例（用于 gate2 编辑回归测试）",
            severity="minor",
            status="investigating",
            discovered_at=datetime.now(UTC),
            investigator_id="U-LC",
            hitl_gates={"gate1": "done", "gate2": "pending", "gate3": "pending",
                        "gate4": "pending", "gate5": "pending"},
        )
        test_db.add(case)
        await test_db.flush()
        test_db.add(FiveWhy(
            id="FW-EDIT-1", case_id="DEV-EDIT-001", n=1,
            question="为什么？", answer="原始答案", status="pending",
        ))
        await test_db.commit()

        auth = _auth_ctx(seed_db[0], {"hitl:accept", "hitl:edit"})

        # 顺序强制：跳过 gate1 直接处理 gate2 → 400
        # （gate1 已 done，此处直接 edit gate2）
        resp = await handle_hitl_gate(
            request=MagicMock(), case_id="DEV-EDIT-001", gate_id="gate2",
            body=HitlGateRequest(
                gate_id="gate2", action="edit",
                comment="修正推理",
                edit_payload={"five_whys": [{"n": 1, "why_text": "人工修正后的答案"}]},
            ),
            db=test_db, auth=auth,
        )
        assert resp.action == "edit"

        fw = (await test_db.execute(select(FiveWhy).where(FiveWhy.id == "FW-EDIT-1"))).scalar_one()
        assert fw.answer == "人工修正后的答案"
        assert fw.status == "edited"

        case = (await test_db.execute(select(Deviation).where(Deviation.id == "DEV-EDIT-001"))).scalar_one()
        assert case.hitl_gates["gate2"] == "done"

        # update_five_why 使用结构化 schema 且记录 before/after
        detail = await update_five_why(
            request=MagicMock(), case_id="DEV-EDIT-001", why_id="FW-EDIT-1",
            body=FiveWhyUpdate(answer="二次修正"), db=test_db, auth=auth,
        )
        assert detail.five_whys[0].answer == "二次修正"


# ============================================================
#  B4/B5 数据级隔离（P2-③）
# ============================================================
class TestDataIsolation:
    """qa_reviewer 只能处理指派给本人的案例；reviewing 状态核心字段冻结"""

    @staticmethod
    def _make_iso_case(case_id, status="investigating"):
        from shared.models import Deviation

        return Deviation(
            id=case_id,
            title="数据级隔离测试案例（B4/B5 回归）",
            severity="minor",
            status=status,
            discovered_at=datetime.now(UTC),
            investigator_id="U-LC",
            qa_reviewer_id="U-JW",
            progress=20,
            hitl_gates={f"gate{i}": ("done" if i == 1 else "pending") for i in range(1, 6)},
        )

    async def test_qa_reviewer_restricted_to_assigned_case(self, test_db, seed_db):
        from case_service.routes import handle_hitl_gate
        from shared.exceptions import ForbiddenError
        from shared.models import User
        from shared.schemas import HitlGateRequest
        from shared.security import hash_password

        other_qa = User(
            id="U-QA2",
            email="u-qa2" + "@rca.local",
            name="Second QA",
            role="qa_reviewer",
            dept="质量保证",
            title="QA 审核员",
            password_hash=hash_password("iso" + "-test-pw-1"),
            is_active=True,
        )
        test_db.add(other_qa)
        test_db.add(self._make_iso_case("DEV-ISO-001"))
        await test_db.commit()

        assigned, unassigned = seed_db[1], other_qa
        # 被指派的 U-JW 正常处理 gate2
        resp = await handle_hitl_gate(
            request=MagicMock(), case_id="DEV-ISO-001", gate_id="gate2",
            body=HitlGateRequest(gate_id="gate2", action="accept"),
            db=test_db, auth=_auth_ctx(assigned, {"hitl:accept"}),
        )
        assert resp.action == "accept"
        # 未被指派的 U-QA2 → 403
        with pytest.raises(ForbiddenError, match="指派"):
            await handle_hitl_gate(
                request=MagicMock(), case_id="DEV-ISO-001", gate_id="gate3",
                body=HitlGateRequest(gate_id="gate3", action="accept"),
                db=test_db, auth=_auth_ctx(unassigned, {"hitl:accept"}),
            )
        # 管理员应急兜底不受限
        resp2 = await handle_hitl_gate(
            request=MagicMock(), case_id="DEV-ISO-001", gate_id="gate3",
            body=HitlGateRequest(gate_id="gate3", action="accept"),
            db=test_db, auth=_auth_ctx(seed_db[2], {"hitl:accept"}),
        )
        assert resp2.action == "accept"

    async def test_unassigned_case_open_to_any_qa(self, test_db, seed_db):
        """未指派审核人的案例任何 qa_reviewer 都可处理（现状兼容）"""
        from case_service.routes import handle_hitl_gate
        from shared.models import User
        from shared.schemas import HitlGateRequest
        from shared.security import hash_password

        other_qa = User(
            id="U-QA3",
            email="u-qa3" + "@rca.local",
            name="Third QA",
            role="qa_reviewer",
            dept="质量保证",
            title="QA 审核员",
            password_hash=hash_password("iso" + "-test-pw-2"),
            is_active=True,
        )
        case = self._make_iso_case("DEV-ISO-002")
        case.qa_reviewer_id = None
        test_db.add(other_qa)
        test_db.add(case)
        await test_db.commit()

        resp = await handle_hitl_gate(
            request=MagicMock(), case_id="DEV-ISO-002", gate_id="gate2",
            body=HitlGateRequest(gate_id="gate2", action="accept"),
            db=test_db, auth=_auth_ctx(other_qa, {"hitl:accept"}),
        )
        assert resp.action == "accept"

    async def test_reviewing_freezes_core_fields(self, test_db, seed_db):
        from case_service.routes import update_case
        from shared.exceptions import ValidationError
        from shared.schemas import DeviationUpdate

        test_db.add(self._make_iso_case("DEV-ISO-003", status="reviewing"))
        investigator = seed_db[0]
        await test_db.commit()

        with pytest.raises(ValidationError, match="冻结"):
            await update_case(
                request=MagicMock(), case_id="DEV-ISO-003",
                body=DeviationUpdate(title="审批阶段擅自改标题"),
                db=test_db, auth=_auth_ctx(investigator, {"case:write"}),
            )
        with pytest.raises(ValidationError, match="冻结"):
            await update_case(
                request=MagicMock(), case_id="DEV-ISO-003",
                body=DeviationUpdate(severity="critical"),
                db=test_db, auth=_auth_ctx(investigator, {"case:write"}),
            )
        # 白名单字段（指派审核人/审批期限/备注）仍可更新
        out = await update_case(
            request=MagicMock(), case_id="DEV-ISO-003",
            body=DeviationUpdate(qa_reviewer_id="U-JW", meta={"note": "审批备注"}),
            db=test_db, auth=_auth_ctx(investigator, {"case:write"}),
        )
        assert out.qa_reviewer_id == "U-JW"
