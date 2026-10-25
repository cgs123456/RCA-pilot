"""Case Service — 偏差案例 CRUD + HITL 关卡 + 电子签名 + Dashboard KPI

注意：本文件不使用 `from __future__ import annotations`（PEP 563）——
slowapi 包装端点的字符串注解解析会落到 slowapi 模块命名空间导致 NameError。
"""
import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import anyio.to_thread
import httpx
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import desc, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shared.audit import append_audit_log
from shared.auth_middleware import AuthContext, get_auth_context, get_db_from_auth, require
from shared.config import settings
from shared.error_codes import ErrorCode
from shared.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationError
from shared.metrics import HITL_DECISIONS_TOTAL
from shared.models import (
    AgentRun,
    Capa,
    Deviation,
    Evidence,
    FiveWhy,
    Report,
    SignatureRecord,
    SkepticChallenge,
)
from shared.notify import create_notification
from shared.rate_limit import api_limit, write_limit
from shared.schemas import (
    CaseReopenRequest,
    DeviationCreate,
    DeviationDetail,
    DeviationOut,
    DeviationTrendPoint,
    DeviationUpdate,
    FiveWhyUpdate,
    HitlGateRequest,
    HitlGateResponse,
    KPIOut,
    Page,
    RiskAssessmentOut,
    RiskAssessmentUpdate,
    SignatureOut,
    SignatureRequest,
    SkepticResolve,
    SourceSystemOut,
)
from shared.security import sha256_hex, verify_password

logger = logging.getLogger("case-service")

router = APIRouter(tags=["case"])

# HITL 关卡定义
HITL_GATES = ["gate1", "gate2", "gate3", "gate4", "gate5"]

# ICH Q9 FMEA 类别阈值 —— RPN≥200 critical / ≥125 high / ≥60 medium，其余 low
_RISK_THRESHOLDS = ((200, "critical"), (125, "high"), (60, "medium"))
_RISK_CATEGORIES = ("critical", "high", "medium", "low")


def derive_risk_category(rpn: int) -> str:
    for threshold, category in _RISK_THRESHOLDS:
        if rpn >= threshold:
            return category
    return "low"


def escalate_severity(severity: str, complexity: int) -> tuple[str, str | None]:
    """ICH Q9 自动严重度升级（shared/models.py Deviation 注释中的规则表）。

    返回 (生效严重度, 触发的规则描述)；未升级时规则描述为 None。
    """
    rules = {
        ("major", 3): ("critical", "Major + 高复杂度(3) → critical"),
        ("minor", 3): ("major", "Minor + 高复杂度(3) → major"),
    }
    rule = rules.get((severity, complexity))
    return (rule[0], rule[1]) if rule else (severity, None)


def is_unique_violation(orig: BaseException | None, e: Exception) -> bool:
    """跨方言判定 IntegrityError 是否唯一/主键冲突（消融实验 E1 实证）。

    - psycopg2：异常类名 UniqueViolation —— locale 无关的主信号
      （PG lc_messages 非 English 时消息与 diag.constraint_name 均不可依赖）；
    - aiosqlite：同为 IntegrityError 类且无 constraint_name，仅消息可辨
      （"UNIQUE constraint failed: ..."）。
    """
    if orig is not None and "uniqueviolation" in type(orig).__name__.lower():
        return True
    return "unique constraint" in str(orig or e).lower()
HITL_GATE_LABELS = {
    "gate1": "证据收集完整性确认",
    "gate2": "5 Whys 推理审查",
    "gate3": "Skeptic 反例审阅",
    "gate4": "CAPA 计划评审",
    "gate5": "报告最终签发",
}

# PATCH 允许显式清空（置 None）的字段 —— 其余可空字段传 None 视为未提供
_CLEARABLE_FIELDS = {
    "description", "location", "area", "shift", "deadline",
    "organism", "organism_source", "product_impact", "immediate_action",
}


# ============================================================
#  偏差 CRUD
# ============================================================
@router.get("/cases", response_model=Page[DeviationOut], summary="查询偏差案例列表")
@api_limit
async def list_cases(
    request: Request,
    status: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    investigator_id: str | None = Query(default=None),
    keyword: str | None = Query(default=None, description="标题/ID 模糊搜索"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db_from_auth),
    _: AuthContext = Depends(require("case:read")),
) -> Page[DeviationOut]:
    """查询偏差案例列表 — 支持多条件筛选与分页"""
    stmt = select(Deviation).options(
        selectinload(Deviation.investigator),
        selectinload(Deviation.qa_reviewer),
    )

    if status:
        stmt = stmt.where(Deviation.status == status)
    if severity:
        stmt = stmt.where(Deviation.severity == severity)
    if investigator_id:
        stmt = stmt.where(Deviation.investigator_id == investigator_id)
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(
            (Deviation.title.ilike(like)) | (Deviation.id.ilike(like))
        )

    # 总数
    count_stmt = select(func.count()).select_from(Deviation)
    if status:
        count_stmt = count_stmt.where(Deviation.status == status)
    if severity:
        count_stmt = count_stmt.where(Deviation.severity == severity)
    if investigator_id:
        count_stmt = count_stmt.where(Deviation.investigator_id == investigator_id)
    if keyword:
        like = f"%{keyword}%"
        count_stmt = count_stmt.where(
            (Deviation.title.ilike(like)) | (Deviation.id.ilike(like))
        )
    total = (await db.execute(count_stmt)).scalar_one()

    stmt = stmt.order_by(desc(Deviation.discovered_at)).offset(
        (page - 1) * page_size
    ).limit(page_size)
    rows = (await db.execute(stmt)).scalars().all()

    return Page[DeviationOut](
        items=[DeviationOut.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/cases/{case_id}", response_model=DeviationDetail, summary="查询案例详情")
@api_limit
async def get_case(
    request: Request,
    case_id: str,
    db: AsyncSession = Depends(get_db_from_auth),
    _: AuthContext = Depends(require("case:read")),
) -> DeviationDetail:
    """查询案例完整信息 — 含证据/时间线/5Whys/Skeptic"""
    stmt = (
        select(Deviation)
        .options(
            selectinload(Deviation.investigator),
            selectinload(Deviation.qa_reviewer),
            selectinload(Deviation.evidence),
            selectinload(Deviation.timeline),
            selectinload(Deviation.five_whys),
            selectinload(Deviation.skeptic_challenges),
        )
        .where(Deviation.id == case_id)
    )
    case = (await db.execute(stmt)).scalar_one_or_none()
    if not case:
        raise NotFoundError(f"案例 {case_id} 不存在")
    return DeviationDetail.model_validate(case)


@router.post("/cases", response_model=DeviationOut, status_code=201, summary="创建偏差案例")
@write_limit
async def create_case(
    body: DeviationCreate,
    request: Request,
    db: AsyncSession = Depends(get_db_from_auth),
    auth: AuthContext = Depends(require("case:write")),
) -> DeviationOut:
    """创建偏差案例 — 自动生成 ID（若未提供）+ 初始化 HITL 关卡 + 审计日志

    ID 并发安全：基于当月已有案例数生成，冲突时自动重试递增（最多 5 次）。
    """
    # 初始化 HITL 关卡
    hitl_gates = {g: "pending" for g in HITL_GATES}

    now = datetime.now(UTC)
    # ICH Q9：严重度按复杂度自动升级（models.py Deviation 规则表），升级留痕进 meta
    effective_severity, escalation_rule = escalate_severity(body.severity, body.complexity)
    meta = dict(body.meta)
    if escalation_rule:
        meta["severity_escalation"] = {
            "original": body.severity,
            "effective": effective_severity,
            "rule": escalation_rule,
        }

    # ICH Q9 FMEA 初评：RPN=S×O×D，类别按阈值派生，分量快照存 meta["fmea"]
    risk_rpn: int | None = None
    risk_category: str | None = None
    if body.fmea:
        risk_rpn = body.fmea.severity_score * body.fmea.occurrence * body.fmea.detection
        risk_category = derive_risk_category(risk_rpn)
        meta["fmea"] = body.fmea.model_dump()

    max_retries = 5
    for _ in range(max_retries):
        if body.id:
            case_id = body.id
        else:
            count_stmt = select(func.count()).select_from(Deviation).where(
                func.to_char(Deviation.discovered_at, "YYYY-MM") == now.strftime("%Y-%m")
            )
            n = (await db.execute(count_stmt)).scalar_one() + 1
            case_id = f"DEV-{now.strftime('%Y-%m')}-{n:03d}"

        case = Deviation(
            id=case_id,
            title=body.title,
            description=body.description,
            severity=effective_severity,
            status="pending",
            deviation_type=body.deviation_type,
            affected_batches=body.affected_batches,
            case_type=body.case_type,
            complexity=body.complexity,
            location=body.location,
            area=body.area,
            shift=body.shift,
            investigator_id=body.investigator_id or auth.user_id,
            qa_reviewer_id=body.qa_reviewer_id,
            discovered_at=body.discovered_at,
            deadline=body.deadline,
            organism=body.organism,
            organism_source=body.organism_source,
            product_impact=body.product_impact,
            immediate_action=body.immediate_action,
            progress=0,
            hitl_gates=hitl_gates,
            meta=meta,
            risk_rpn=risk_rpn,
            risk_category=risk_category,
            risk_owner=body.risk_owner,
        )
        db.add(case)
        try:
            await db.flush()
            break
        except IntegrityError as e:
            await db.rollback()
            orig = getattr(e, "orig", None)
            if is_unique_violation(orig, e):
                if body.id:
                    raise ValidationError(
                        f"案例 ID {case_id} 已存在",
                        error_code=None,
                    ) from e
                continue  # 自动生成 ID 冲突 → 重新计数再试
            # 外键违反（investigator_id/qa_reviewer_id/risk_owner 指向的用户不存在）
            raise ValidationError(
                "关联的用户不存在（investigator_id / qa_reviewer_id / risk_owner 校验失败）",
                error_code=None,
            ) from e
    else:
        raise ConflictError("案例 ID 生成冲突，请重试")

    # P2-⑧ 创建时即指定审核人 → 通知（自指派不打扰）
    if body.qa_reviewer_id and body.qa_reviewer_id != auth.user_id:
        await create_notification(
            db,
            body.qa_reviewer_id,
            "CASE_ASSIGNED",
            case_id=case_id,
            payload={"title": body.title, "by": auth.email},
        )

    audit_parts = [f"创建偏差案例: {body.title}"]
    if escalation_rule:
        audit_parts.append(f"严重度自动升级: {body.severity}→{effective_severity}（{escalation_rule}）")
    if risk_rpn is not None:
        audit_parts.append(f"FMEA 初评 RPN={risk_rpn}（{risk_category}）")
    await append_audit_log(
        db,
        operator=auth.email,
        action="CREATE_DEVIATION",
        target=case.id,
        detail="；".join(audit_parts),
        ip_address=auth.ip_address,
        session_id=auth.session_id,
    )

    # 重新加载 case 并 eagerly load investigator 关系（避免 async lazy-load 报错）
    case = (
        await db.execute(
            select(Deviation)
            .options(selectinload(Deviation.investigator))
            .where(Deviation.id == case.id)
        )
    ).scalar_one()
    return DeviationOut.model_validate(case)


@router.patch(
    "/cases/{case_id}/risk",
    response_model=RiskAssessmentOut,
    summary="更新 ICH Q9 风险评估（FMEA）",
)
@write_limit
async def update_risk_assessment(
    case_id: str,
    body: RiskAssessmentUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db_from_auth),
    auth: AuthContext = Depends(require("case:write")),
) -> RiskAssessmentOut:
    """重评 RPN=S×O×D 并按阈值派生风险类别；分量快照与变更前后值均留审计。"""
    case = (
        await db.execute(select(Deviation).where(Deviation.id == case_id).with_for_update())
    ).scalar_one_or_none()
    if not case:
        raise NotFoundError(f"案例 {case_id} 不存在")
    if case.status == "closed":
        raise ValidationError("已结案的案例不可修改风险评估；如需修订请重新开案走受控变更流程", error_code=None)

    old_rpn, old_category = case.risk_rpn, case.risk_category
    rpn = body.fmea.severity_score * body.fmea.occurrence * body.fmea.detection
    category = derive_risk_category(rpn)
    case.risk_rpn = rpn
    case.risk_category = category
    if body.risk_owner is not None:
        case.risk_owner = body.risk_owner
    case.meta = {**(case.meta or {}), "fmea": body.fmea.model_dump()}

    await append_audit_log(
        db,
        operator=auth.email,
        action="RISK_ASSESSMENT_UPDATED",
        target=case.id,
        detail=f"ICH Q9 风险评估更新: RPN {old_rpn}→{rpn}，类别 {old_category}→{category}",
        ip_address=auth.ip_address,
        session_id=auth.session_id,
    )
    return RiskAssessmentOut(risk_rpn=rpn, risk_category=category, risk_owner=case.risk_owner, fmea=body.fmea)


@router.patch("/cases/{case_id}", response_model=DeviationOut, summary="更新偏差案例")
@write_limit
async def update_case(
    request: Request,
    case_id: str,
    body: DeviationUpdate,
    db: AsyncSession = Depends(get_db_from_auth),
    auth: AuthContext = Depends(require("case:write")),
) -> DeviationOut:
    """更新偏差案例字段 — 仅允许调查中/reviewing 状态修改

    状态机约束：
    - 已结案 (closed) 案例不可修改任何字段
    - status=closed 不允许通过 PATCH 设置 —— 必须走 /signatures 端点完成双人电子签名后由系统自动结案
    - status=reviewing 仅在 HITL 5 关全通过时由系统设置
    - progress 由系统根据 HITL 关卡自动推进，不可手工指定
    """
    case = (
        await db.execute(
            select(Deviation)
            .options(selectinload(Deviation.investigator))
            .where(Deviation.id == case_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not case:
        raise NotFoundError(f"案例 {case_id} 不存在")
    if case.status == "closed":
        raise ValidationError(
            "已结案的案例不可修改",
            error_code=ErrorCode.CASE_ALREADY_CLOSED.value,
        )

    # 禁止通过 PATCH 直接设置 status=closed —— 必须走 /signatures 双签流程
    if body.status == "closed":
        raise ValidationError(
            "不允许通过 PATCH 直接结案；请通过 POST /cases/{case_id}/signatures "
            "（signature_meaning=approve）完成 QA 双签后系统自动结案",
            error_code=ErrorCode.CASE_STATUS_TRANSITION_INVALID.value,
        )

    # 禁止通过 PATCH 手动设置 reviewing —— 该状态仅由 HITL 5 关全通过触发
    if body.status == "reviewing":
        raise ValidationError(
            "reviewing 状态由系统在 HITL 5 关全部通过后自动设置，不可手动指定",
            error_code=ErrorCode.CASE_STATUS_TRANSITION_INVALID.value,
        )

    provided = body.model_dump(exclude_unset=True)

    # B5 reviewing 字段白名单：进入审批阶段后调查核心字段冻结，
    # 仅允许审批流程相关变更（指派审核人/审批期限/备注）。
    if case.status == "reviewing":
        reviewing_allowed = {"qa_reviewer_id", "deadline", "meta"}
        frozen = sorted(set(provided) - reviewing_allowed - {"status", "progress"})
        if frozen:
            raise ValidationError(
                f"案例处于 reviewing（审批）状态，核心字段已冻结：{', '.join(frozen)}；"
                "仅允许更新指派的 QA 审核人/审批期限/备注。字段修订请走报告版本化或重新开案流程",
            )

    # progress 由系统计算（HITL 关卡推进），绕过 gate 直接改进度属于合规违规
    if "progress" in provided:
        raise ValidationError("progress 由系统根据 HITL 关卡自动推进，不可通过 PATCH 修改")

    prev_qa_reviewer = case.qa_reviewer_id
    changes = []
    for field, value in provided.items():
        # status 字段只允许 pending → investigating 的正向流转
        if field == "status":
            if value != "investigating":
                raise ValidationError(
                    "PATCH 仅允许将 status 改为 investigating；其他状态变更请走专用端点"
                )
            if case.status not in ("pending",):
                raise ValidationError(
                    f"当前状态 {case.status} 不允许通过 PATCH 改为 {value}"
                )
        # 显式清空（None）：仅白名单字段允许，防止 exclude_unset 语义被丢弃后误清关键字段
        if value is None and field not in _CLEARABLE_FIELDS:
            continue
        # meta 深合并：系统在 fmea（风险评估）/gate3_retry_count（自动重试）/
        # reopen（重开留痕）等处合并写 meta，客户端整体替换会把这些系统键静默抹掉
        if field == "meta" and isinstance(value, dict):
            old_meta = dict(case.meta or {})
            case.meta = {**old_meta, **value}
            if case.meta != old_meta:
                changes.append(f"meta 合并写入键: {sorted(value)}")
            continue
        old = getattr(case, field)
        if old != value:
            changes.append(f"{field}: {old!r} → {value!r}")
            setattr(case, field, value)

    # P2-⑧ 重新指派 QA 审核人 → 通知新审核人（未变更/自指派不打扰）
    if (
        "qa_reviewer_id" in provided
        and provided["qa_reviewer_id"]
        and provided["qa_reviewer_id"] != prev_qa_reviewer
        and provided["qa_reviewer_id"] != auth.user_id
    ):
        await create_notification(
            db,
            provided["qa_reviewer_id"],
            "CASE_ASSIGNED",
            case_id=case.id,
            payload={"title": case.title, "by": auth.email},
        )

    if changes:
        await append_audit_log(
            db,
            operator=auth.email,
            action="UPDATE_DEVIATION",
            target=case_id,
            detail="; ".join(changes),
            ip_address=auth.ip_address,
            session_id=auth.session_id,
        )

    await db.flush()
    return DeviationOut.model_validate(case)


# ============================================================
#  HITL 关卡管理
# ============================================================
def _progress_for(gates_done: int) -> int:
    return min(100, gates_done * 20)


@router.put(
    "/cases/{case_id}/hitl/{gate_id}",
    response_model=HitlGateResponse,
    summary="处理 HITL 关卡 (accept/reject/edit)",
)
@write_limit
async def handle_hitl_gate(
    request: Request,
    case_id: str,
    gate_id: str,
    body: HitlGateRequest,
    db: AsyncSession = Depends(get_db_from_auth),
    auth: AuthContext = Depends(require("hitl:accept")),
) -> HitlGateResponse:
    """处理 HITL 关卡 — accept/reject/edit

    - gate_id 必须为 gate1~gate5，且必须按顺序处理（gateN 的前置关卡须全部 done）
    - gate2 支持 edit：edit_payload = {"five_whys": [{"n": 1, "why_text": "..."}]}
      —— 编辑后写入 five_whys 记录并记录 before/after 审计
    - gate3 reject → 若 hitl_gate3_auto_retry flag 启用且未达上限，真实触发
      Agent Service 重启流水线（服务间调用），gate2/gate3 重置为 pending
    - 全部 5 关通过 → 案例进入 reviewing 状态
    """
    if gate_id != body.gate_id:
        raise ValidationError("路径 gate_id 与请求体不一致")
    if gate_id not in HITL_GATES:
        raise ValidationError(f"无效的 gate_id，应为 {HITL_GATES}")

    # 行锁查询案例 —— 防止两人同时操作不同 gate 时 JSONB 读改写互相覆盖
    stmt = (
        select(Deviation)
        .options(selectinload(Deviation.investigator), selectinload(Deviation.five_whys))
        .where(Deviation.id == case_id)
        .with_for_update()
    )
    case = (await db.execute(stmt)).scalar_one_or_none()
    if not case:
        raise NotFoundError(f"案例 {case_id} 不存在")
    if case.status == "closed":
        raise ValidationError("已结案的案例不可再处理 HITL 关卡")

    # B4 数据级隔离：qa_reviewer 只能处理指派给本人的案例。
    # 调查执行人（investigator）推进关卡属正常流程，管理员为应急兜底，均不受此限。
    if (
        case.qa_reviewer_id
        and auth.role == "qa_reviewer"
        and auth.user_id != case.qa_reviewer_id
    ):
        raise ForbiddenError(
            f"本案例已指派给 QA 审核人 {case.qa_reviewer_id}，其他审核人不能代为处理 HITL 关卡",
        )

    hitl = dict(case.hitl_gates or {})

    # ---- 顺序强制：前置关卡必须全部 done ----
    idx = HITL_GATES.index(gate_id)
    prior = HITL_GATES[:idx]
    not_done = [g for g in prior if hitl.get(g) != "done"]
    if not_done:
        raise ValidationError(
            f"HITL 关卡必须按顺序处理：{', '.join(not_done)} 尚未通过",
            error_code=ErrorCode.HITL_GATE_INVALID.value,
        )
    if hitl.get(gate_id) == "done":
        raise ValidationError(
            f"{gate_id} 已通过，不可重复处理",
            error_code=ErrorCode.HITL_GATE_ALREADY_DONE.value,
        )

    old_state = hitl.get(gate_id, "pending")

    # ---- edit 分支：当前仅 gate2（5 Whys）支持 ----
    if body.action == "edit":
        if gate_id != "gate2":
            raise ValidationError("edit 仅支持 gate2（5 Whys 推理审查）")
        # edit 是独立写权限：端点级 require("hitl:accept") 只覆盖 accept/reject，
        # 持 accept 而无 edit 的角色（如 qa_reviewer）不得借道本端点改写 5 Whys
        auth.check("hitl:edit")
        payload = body.edit_payload or {}
        whys = payload.get("five_whys")
        if not isinstance(whys, list) or not whys:
            raise ValidationError('edit_payload 需包含非空 five_whys 数组：[{"n":1,"why_text":"..."}]')
        fw_by_n = {fw.n: fw for fw in case.five_whys}
        edits = []
        for item in whys:
            n = item.get("n")
            text = item.get("why_text")
            if n not in fw_by_n or not text:
                raise ValidationError(f"无效的 five_whys 条目: {item!r}")
            fw = fw_by_n[n]
            old_answer = fw.answer
            fw.answer = text
            fw.status = "edited"
            edits.append(f"#{n}: {old_answer!r} → {text!r}")
        hitl[gate_id] = "done"
        case.hitl_gates = hitl
        case.progress = max(case.progress, _progress_for(sum(1 for g in HITL_GATES if hitl.get(g) == "done")))
        await append_audit_log(
            db,
            operator=auth.email,
            action="HITL_EDIT",
            target=f"{case_id}/{gate_id}",
            detail=f"{HITL_GATE_LABELS[gate_id]} 编辑 5 Whys — " + "; ".join(edits),
            ip_address=auth.ip_address,
            session_id=auth.session_id,
        )
        HITL_DECISIONS_TOTAL.labels(gate=gate_id, action="edit").inc()
        all_passed = all(hitl.get(g) == "done" for g in HITL_GATES)
        if all_passed:
            case.status = "reviewing"
        return HitlGateResponse(
            case_id=case_id,
            gate_id=gate_id,
            action="edit",
            new_status=case.status,
            all_gates_passed=all_passed,
            triggered_agent=None,
        )

    if body.action not in ("accept", "reject"):
        raise ValidationError("action 仅支持 accept/reject/edit")

    # ---- accept / reject ----
    hitl[gate_id] = "done" if body.action == "accept" else "rejected"
    case.hitl_gates = hitl

    # 进度按已通过的关卡数重算（系统管控，绕过手工设置）
    gates_done = sum(1 for g in HITL_GATES if hitl.get(g) == "done")
    if body.action == "accept":
        case.progress = max(case.progress, _progress_for(gates_done))

    # 状态流转
    new_status = case.status
    all_passed = all(hitl.get(g) == "done" for g in HITL_GATES)
    if all_passed:
        case.status = "reviewing"
        new_status = "reviewing"
    elif gate_id == "gate1" and case.status == "pending":
        case.status = "investigating"
        new_status = "investigating"

    # gate3 reject → 真实触发 Reasoner 自动重试（服务间调用 Agent Service）
    triggered_agent = None
    if gate_id == "gate3" and body.action == "accept":
        triggered_agent = "reasoner"  # 标记进入下一阶段（Skeptic 结论已确认）
    elif gate_id == "gate3" and body.action == "reject":
        from shared.feature_flag import flag_context, is_enabled

        meta = dict(case.meta or {})
        retries = int(meta.get("gate3_retry_count", 0))
        max_retries = int(flag_context("hitl_gate3_auto_retry").get("max_retries", 3))
        if is_enabled("hitl_gate3_auto_retry", user_id=auth.user_id) and retries < max_retries:
            meta["gate3_retry_count"] = retries + 1
            case.meta = meta
            # 重置 gate2/gate3 —— 重启的流水线会重新生成 5 Whys 与 Skeptic 挑战
            hitl["gate2"] = "pending"
            hitl["gate3"] = "pending"
            case.hitl_gates = hitl
            all_passed = False
            case.status = "investigating" if case.status == "reviewing" else case.status
            new_status = case.status
            triggered_agent = "reasoner"
            # B11：服务间调用不得发生在事务内 —— 先提交业务变更（释放行锁、
            # 让 Agent Service 读到已提交的 gate 状态），再派发并按结果审计
            await db.commit()
            dispatch = await _trigger_pipeline_retry(case_id=case_id, operator=auth.email)
            if dispatch == "failed":
                await append_audit_log(
                    db,
                    operator=auth.email,
                    action="HITL_GATE3_AUTO_RETRY_DISPATCH_FAILED",
                    target=f"{case_id}/gate3",
                    detail=(
                        f"gate3 reject 重试派发失败（第 {retries + 1}/{max_retries} 次），"
                        f"gate2/gate3 已重置但流水线未自动重启 —— 请人工在调查页重新发起"
                    ),
                    ip_address=auth.ip_address,
                    session_id=auth.session_id,
                )
            else:
                note = (
                    "服务间凭证未配置，需人工重新发起"
                    if dispatch == "skipped"
                    else f"第 {retries + 1}/{max_retries} 次"
                )
                await append_audit_log(
                    db,
                    operator=auth.email,
                    action="HITL_GATE3_AUTO_RETRY_TRIGGERED",
                    target=f"{case_id}/gate3",
                    detail=(
                        f"gate3 reject 触发 Reasoner 自动重试（{note}），"
                        f"gate2/gate3 已重置为 pending"
                    ),
                    ip_address=auth.ip_address,
                    session_id=auth.session_id,
                )
        elif retries >= max_retries:
            await append_audit_log(
                db,
                operator=auth.email,
                action="HITL_GATE3_AUTO_RETRY_LIMIT",
                target=f"{case_id}/gate3",
                detail=f"gate3 reject 未触发重试：已达上限 {max_retries} 次",
                ip_address=auth.ip_address,
                session_id=auth.session_id,
            )

    # gate4 accept：CAPA 计划评审通过 → 若案例尚无报告版本，
    # 自动把最近一次 Drafter 输出快照为报告 v1（激活 reports 表，版本化起点）
    if gate_id == "gate4" and body.action == "accept":
        await _snapshot_drafter_report_v1(db, case=case, operator=auth.email,
                                         ip_address=auth.ip_address, session_id=auth.session_id)

    await append_audit_log(
        db,
        operator=auth.email,
        action=f"HITL_{body.action.upper()}",
        target=f"{case_id}/{gate_id}",
        detail=f"{HITL_GATE_LABELS[gate_id]} — {body.action} — {body.comment or ''} — {old_state} → {hitl.get(gate_id)}",
        ip_address=auth.ip_address,
        session_id=auth.session_id,
    )
    HITL_DECISIONS_TOTAL.labels(gate=gate_id, action=body.action).inc()

    return HitlGateResponse(
        case_id=case_id,
        gate_id=gate_id,
        action=body.action,
        new_status=new_status,
        all_gates_passed=all_passed,
        triggered_agent=triggered_agent,
    )


async def _snapshot_drafter_report_v1(
    db: AsyncSession,
    *,
    case: Deviation,
    operator: str,
    ip_address: str | None,
    session_id: str | None,
) -> None:
    """gate4 通过时把最近一次 Drafter 报告快照为 Report v1（幂等：已有版本则跳过）

    Drafter 输出保存在 AgentRun.agent_states["drafter"]["report_markdown"]
    （rule 与 LLM 路径同构，见 agent_service/routes.py:_run_drafter）。
    """
    has_report = (
        await db.execute(select(Report.id).where(Report.case_id == case.id).limit(1))
    ).scalar_one_or_none()
    if has_report is not None:
        return
    drafter_state = (
        await db.execute(
            select(AgentRun)
            .where(AgentRun.case_id == case.id, AgentRun.status == "completed")
            .order_by(AgentRun.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    report_markdown = ((drafter_state.agent_states or {}).get("drafter") or {}).get("report_markdown") if drafter_state else None
    if not report_markdown:
        return
    snapshot = Report(
        id=f"RPT-{uuid4().hex[:12].upper()}",
        case_id=case.id,
        version=1,
        title=f"偏差调查报告 — {case.id}",
        content=report_markdown,
        status="draft",
        author_id=case.investigator_id,
        change_reason="gate4 CAPA 计划评审通过时自动快照 Drafter 报告（v1）",
    )
    db.add(snapshot)
    await append_audit_log(
        db,
        operator=operator,
        action="REPORT_VERSION_CREATED",
        target=f"{case.id}/reports/v1",
        detail="gate4 通过，Drafter 报告自动快照为 v1（草稿，待 QA 审查批准）",
        ip_address=ip_address,
        session_id=session_id,
    )


async def _trigger_pipeline_retry(*, case_id: str, operator: str) -> str:
    """调用 Agent Service 重启流水线（服务间调用，X-Service-Key 鉴权）

    返回 dispatched | skipped | failed —— 由调用方写入对应审计；
    派发失败降级为人工重启，不阻塞 HITL 主流程（B11：失败必须显式留痕）。
    """
    if not settings.service_api_key:
        return "skipped"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{settings.agent_service_url}/agent/runs",
                json={"case_id": case_id, "triggered_by": f"system:gate3_auto_retry({operator})"},
                headers={"X-Service-Key": settings.service_api_key},
            )
            resp.raise_for_status()
        return "dispatched"
    except Exception:
        logger.warning("gate3 自动重试派发失败 case=%s，降级为人工重启", case_id, exc_info=True)
        return "failed"


# ============================================================
#  5 Whys
# ============================================================
@router.put(
    "/cases/{case_id}/five-whys/{why_id}",
    response_model=DeviationDetail,
    summary="更新 5 Whys 条目 (HITL gate2 编辑)",
)
@write_limit
async def update_five_why(
    request: Request,
    case_id: str,
    why_id: str,
    body: FiveWhyUpdate,
    db: AsyncSession = Depends(get_db_from_auth),
    auth: AuthContext = Depends(require("hitl:edit")),
) -> DeviationDetail:
    """更新 5 Whys 条目 — 用于 HITL gate2 人工编辑（记录 before/after 审计）"""
    # 行锁查案例：与结案签名并发互斥；结案后根因记录属受控 GxP 文档，一律拒绝直改
    case = (await db.execute(
        select(Deviation).where(Deviation.id == case_id).with_for_update()
    )).scalar_one_or_none()
    if not case:
        raise NotFoundError(f"案例 {case_id} 不存在")
    if case.status == "closed":
        raise ValidationError(
            "已结案的案例不可修改 5 Whys；如需修订请重新开案走受控变更流程",
            error_code=ErrorCode.CASE_ALREADY_CLOSED.value,
        )
    fw = (await db.execute(
        select(FiveWhy).where(FiveWhy.id == why_id, FiveWhy.case_id == case_id)
    )).scalar_one_or_none()
    if not fw:
        raise NotFoundError(f"5 Whys 条目 {why_id} 不存在")

    provided = body.model_dump(exclude_unset=True)

    # evidence_ids 必须真实存在且属于本案例 —— 防止悬挂引用/跨案例证据污染调查记录
    if "evidence_ids" in provided:
        ids = provided["evidence_ids"] or []
        known: set[str] = set()
        if ids:
            known = set((await db.execute(
                select(Evidence.id).where(Evidence.case_id == case_id, Evidence.id.in_(ids))
            )).scalars().all())
        unknown = sorted(set(ids) - known)
        if unknown:
            raise ValidationError(f"evidence_ids 含无效或不属于本案例的证据: {', '.join(unknown)}")

    changes = []
    for field in ("question", "answer", "status", "evidence_ids"):
        if field in provided:
            old = getattr(fw, field)
            new = provided[field]
            if old != new:
                changes.append(f"{field}: {old!r} → {new!r}")
                setattr(fw, field, new)

    await append_audit_log(
        db,
        operator=auth.email,
        action="HITL_EDIT",
        target=f"{case_id}/five-why/{why_id}",
        detail=f"人工编辑 5 Whys #{fw.n} — " + ("; ".join(changes) or "无变化"),
        ip_address=auth.ip_address,
        session_id=auth.session_id,
    )

    # 返回完整案例详情
    stmt = (
        select(Deviation)
        .options(
            selectinload(Deviation.investigator),
            selectinload(Deviation.qa_reviewer),
            selectinload(Deviation.evidence),
            selectinload(Deviation.timeline),
            selectinload(Deviation.five_whys),
            selectinload(Deviation.skeptic_challenges),
        )
        .where(Deviation.id == case_id)
    )
    case = (await db.execute(stmt)).scalar_one()
    return DeviationDetail.model_validate(case)


# ============================================================
#  Skeptic 挑战
# ============================================================
@router.put(
    "/cases/{case_id}/skeptic/{challenge_id}",
    response_model=HitlGateResponse,
    summary="处理 Skeptic 反例挑战 (gate3)",
)
@write_limit
async def resolve_skeptic(
    request: Request,
    case_id: str,
    challenge_id: str,
    body: SkepticResolve,
    db: AsyncSession = Depends(get_db_from_auth),
    auth: AuthContext = Depends(require("hitl:accept")),
) -> HitlGateResponse:
    """处理 Skeptic 反例 — accepted/rejected + 解决方案（记录审计）"""
    # 行锁查案例并先做状态校验 —— 结案后挑战结论属受控 GxP 记录，一律拒绝；也防与结案签名并发
    case = (await db.execute(
        select(Deviation).where(Deviation.id == case_id).with_for_update()
    )).scalar_one_or_none()
    if case is None:
        raise NotFoundError(f"案例 {case_id} 不存在")
    if case.status == "closed":
        raise ValidationError(
            "已结案的案例不可处理 Skeptic 挑战",
            error_code=ErrorCode.CASE_ALREADY_CLOSED.value,
        )
    sc = (await db.execute(
        select(SkepticChallenge).where(
            SkepticChallenge.id == challenge_id,
            SkepticChallenge.case_id == case_id,
        )
    )).scalar_one_or_none()
    if not sc:
        raise NotFoundError(f"Skeptic 挑战 {challenge_id} 不存在")

    old_status = sc.status
    sc.status = body.status
    sc.resolution = body.resolution
    sc.resolved_at = datetime.now(UTC)

    hitl = dict(case.hitl_gates or {})
    all_passed = all(hitl.get(g) == "done" for g in HITL_GATES)
    new_status = "reviewing" if all_passed else case.status

    await append_audit_log(
        db,
        operator=auth.email,
        action=f"SKEPTIC_{body.status.upper()}",
        target=f"{case_id}/{challenge_id}",
        detail=f"status: {old_status} → {body.status}; resolution: {body.resolution}",
        ip_address=auth.ip_address,
        session_id=auth.session_id,
    )

    return HitlGateResponse(
        case_id=case_id,
        gate_id="gate3",
        action="accept" if body.status == "accepted" else "reject",
        new_status=new_status,
        all_gates_passed=all_passed,
        triggered_agent=None,
    )


# ============================================================
#  电子签名
# ============================================================
@router.post(
    "/cases/{case_id}/signatures",
    response_model=SignatureOut,
    status_code=201,
    summary="提交电子签名 (21 CFR Part 11)",
)
@write_limit
async def sign_case(
    request: Request,
    case_id: str,
    body: SignatureRequest,
    db: AsyncSession = Depends(get_db_from_auth),
    auth: AuthContext = Depends(get_auth_context),
) -> SignatureOut:
    """提交电子签名

    - signature_meaning=submit: 调查员提交（要求 sign:submit 权限，案例须处于 reviewing）
    - signature_meaning=approve: QA 批准（要求 sign:approve 权限，须存在他人 submit 签名）
    - 强制顺序：submit → approve（双人签名，禁止同一人双签）
    - 二次密码确认（bcrypt 校验放线程池）
    """
    # 二次密码确认（CPU 密集 → 线程池）
    password_ok = await anyio.to_thread.run_sync(verify_password, body.password, auth.user.password_hash)
    if not password_ok:
        raise ForbiddenError(
            "密码确认失败",
            error_code=ErrorCode.SIGNATURE_PASSWORD_WRONG.value,
        )

    # 权限校验
    if body.signature_meaning == "submit":
        auth.check("sign:submit")
    elif body.signature_meaning == "approve":
        auth.check("sign:approve")

    case = (
        await db.execute(
            select(Deviation).where(Deviation.id == case_id).with_for_update()
        )
    ).scalar_one_or_none()
    if not case:
        raise NotFoundError(f"案例 {case_id} 不存在")

    if case.status == "closed":
        raise ValidationError(
            "案例已结案，不可再次签署",
            error_code=ErrorCode.CASE_ALREADY_CLOSED.value,
        )

    # 受控修订轮次：reopen 时递增并写入 meta.reopen.count；
    # 签名查重/主键仅作用于当前修订轮次（旧轮次签名记录不可篡改地保留），
    # 否则 reopen 后同一签字人永远无法重新提交/批准（重签死锁）
    revision = int((case.meta or {}).get("reopen", {}).get("count", 0) or 0)

    existing_sigs = (
        await db.execute(
            select(SignatureRecord).where(
                SignatureRecord.case_id == case_id,
                SignatureRecord.revision == revision,
            )
        )
    ).scalars().all()
    submit_sig = next((s for s in existing_sigs if s.signature_meaning == "提交签名"), None)

    if body.signature_meaning == "submit":
        if case.status != "reviewing":
            raise ValidationError(
                f"案例状态为 {case.status}，调查员提交签名要求案例处于 reviewing（HITL 5 关全部通过）",
                error_code=ErrorCode.SIGNATURE_REQUIRED.value,
            )
        if submit_sig is not None:
            raise ConflictError(
                "该案例已存在提交签名，不可重复提交",
                error_code=ErrorCode.SIGNATURE_REQUIRED.value,
            )
    else:  # approve
        if case.status != "reviewing":
            raise ValidationError(
                f"案例状态为 {case.status}，批准签名要求案例处于 reviewing",
                error_code=ErrorCode.SIGNATURE_REQUIRED.value,
            )
        if submit_sig is None:
            raise ValidationError(
                "尚未收到调查员的提交签名（submit），不可直接批准 —— 强制顺序 submit → approve",
                error_code=ErrorCode.SIGNATURE_DUAL_REQUIRED.value,
            )
        if submit_sig.signer_id == auth.user_id:
            raise ValidationError(
                "提交人与批准人必须为不同用户（21 CFR Part 11 双人签名）",
                error_code=ErrorCode.SIGNATURE_DUAL_REQUIRED.value,
            )
        # 指定审核人一致性：建案时指派了 qa_reviewer_id 则批准必须由其本人执行；
        # 如需换人，走案例更新重新指派（留审计痕迹），而非任何人凭 sign:approve 越俎代庖
        if case.qa_reviewer_id and auth.user_id != case.qa_reviewer_id:
            raise ForbiddenError(
                f"本案例的指定 QA 审核人为 {case.qa_reviewer_id}，批准签名须由其本人执行",
                error_code=ErrorCode.SIGNATURE_DUAL_REQUIRED.value,
            )

    signed_at = datetime.now(UTC)
    sig_id = f"SIG-{case_id}-{body.signature_meaning.upper()}-{auth.user_id}"

    # 证书指纹：对签名内容（案例+含义+签字人+时间戳）做 HMAC，可追溯到本次签名行为；
    # 接入真实数字证书后可替换为证书指纹
    fingerprint_src = f"{case_id}|{body.signature_meaning}|{auth.user_id}|{signed_at.isoformat()}"
    fingerprint = "sha256:" + sha256_hex(fingerprint_src + settings.jwt_secret)

    sig = SignatureRecord(
        id=sig_id,
        case_id=case_id,
        signer_id=auth.user_id,
        signer_role=auth.user.role,
        signature_meaning="提交签名" if body.signature_meaning == "submit" else "批准签名",
        certificate_fingerprint=fingerprint,
        ip_address=auth.ip_address,
        confirmation_text=body.confirmation_text,
        signed_at=signed_at,
    )
    db.add(sig)

    # 双签完成后结案
    if body.signature_meaning == "approve":
        case.status = "closed"
        case.progress = 100
        case.closed_at = signed_at
        # P2-⑧ 结案通知调查员与 QA 审核人（签署人本人不打扰）
        for recipient in {case.investigator_id, case.qa_reviewer_id} - {auth.user_id, None}:
            await create_notification(
                db,
                recipient,
                "CASE_CLOSED",
                case_id=case_id,
                payload={"title": case.title, "by": auth.email},
            )

    # 先单独 flush sig 并捕获主键/唯一约束冲突 -> 返回 409 而非 500。
    # 注意：不能在写审计之后才 try/except —— append_audit_log 内部会再 flush
    # 一次并提前把 sig 的 IntegrityError 抛出来，导致下方 409 分支成为不可达死代码。
    try:
        await db.flush()
    except IntegrityError as e:
        raise ConflictError(
            "签名记录冲突（同一用户不可对同一案例重复签署）",
            error_code=ErrorCode.SIGNATURE_REQUIRED.value,
        ) from e

    await append_audit_log(
        db,
        operator=auth.email,
        action=f"SIGN_{body.signature_meaning.upper()}",
        target=case_id,
        detail=(
            f"{auth.user.name} ({auth.user.role}) 完成"
            f"{'提交' if body.signature_meaning == 'submit' else '批准'}签名"
            f"{'，案例结案' if body.signature_meaning == 'approve' else ''}"
        ),
        ip_address=auth.ip_address,
        session_id=auth.session_id,
    )

    return SignatureOut.model_validate(sig)


# ============================================================
#  结案重审（reopen）
# ============================================================
@router.post(
    "/cases/{case_id}/reopen",
    response_model=DeviationOut,
    summary="结案重审：closed → reviewing，走受控修订后重新签署",
)
@write_limit
async def reopen_case(
    request: Request,
    case_id: str,
    body: CaseReopenRequest,
    db: AsyncSession = Depends(get_db_from_auth),
    auth: AuthContext = Depends(get_auth_context),
) -> DeviationOut:
    """结案重审（21 CFR Part 11 受控修订入口）

    - 权限：sign:approve（QA 批准人发起重审，与批准签名同一责任人）
    - 仅 closed 案例可重开；重开后案例回到 reviewing，旧签名记录保留（不可篡改）
    - 修订完成后必须重新走 submit → approve 双人签名（sign_case 既有校验自然生效）
    """
    auth.check("sign:approve")

    case = (
        await db.execute(
            select(Deviation).where(Deviation.id == case_id).with_for_update()
        )
    ).scalar_one_or_none()
    if not case:
        raise NotFoundError(f"案例 {case_id} 不存在")

    if case.status != "closed":
        raise ConflictError(
            f"案例状态为 {case.status}，仅已结案（closed）案例可申请重审",
        )

    case.status = "reviewing"
    case.progress = 90
    meta = dict(case.meta or {})
    meta["reopen"] = {
        "reason": body.reason,
        "reopened_by": auth.user_id,
        "reopened_at": datetime.now(UTC).isoformat(),
    }
    case.meta = meta

    await append_audit_log(
        db,
        operator=auth.email,
        action="CASE_REOPENED",
        target=case_id,
        detail=f"结案重审：{auth.user.name} 申请修订，原因：{body.reason}；旧签名记录保留，修订后须重新双人签名",
        ip_address=auth.ip_address,
        session_id=auth.session_id,
    )
    return DeviationOut.model_validate(case)


# ============================================================
#  Dashboard KPI
# ============================================================
@router.get("/dashboard/kpi", response_model=KPIOut, summary="Dashboard KPI 指标")
@api_limit
async def get_kpi(
    request: Request,
    db: AsyncSession = Depends(get_db_from_auth),
    _: AuthContext = Depends(require("case:read")),
) -> KPIOut:
    """计算 Dashboard 关键指标（全部实算，不再硬编码 0）"""
    now = datetime.now(UTC)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_month_start = (month_start - timedelta(days=1)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    week_ago = now - timedelta(days=7)

    # 未解决案例数
    unsolved = (await db.execute(
        select(func.count()).select_from(Deviation).where(
            Deviation.status != "closed"
        )
    )).scalar_one()

    # 本月重大偏差数
    major_count = (await db.execute(
        select(func.count()).select_from(Deviation).where(
            Deviation.severity == "major",
            Deviation.discovered_at >= month_start,
        )
    )).scalar_one()
    # 上月重大偏差数（趋势）
    major_count_last_month = (await db.execute(
        select(func.count()).select_from(Deviation).where(
            Deviation.severity == "major",
            Deviation.discovered_at >= last_month_start,
            Deviation.discovered_at < month_start,
        )
    )).scalar_one()

    # 平均闭环天数：优先 closed_at（任何 PATCH 都会刷新 updated_at，旧口径失真）
    cycle_expr = func.coalesce(
        Deviation.closed_at,
        Deviation.updated_at,  # 兼容 closed_at 为空的历史数据
    )
    closed_avg = (await db.execute(
        select(
            func.avg(
                func.extract("epoch", cycle_expr - Deviation.discovered_at) / 86400
            )
        ).where(Deviation.status == "closed")
    )).scalar_one()
    avg_cycle = float(closed_avg or 0)

    # 近 30 天闭环平均（趋势对比用）
    closed_avg_30d = (await db.execute(
        select(
            func.avg(
                func.extract("epoch", cycle_expr - Deviation.discovered_at) / 86400
            )
        ).where(
            Deviation.status == "closed",
            cycle_expr >= now - timedelta(days=30),
        )
    )).scalar_one()
    avg_cycle_30d = float(closed_avg_30d or 0) or avg_cycle

    # 3.2-1 口径对齐：capa_overdue_rate 按 CAPA 实体计算（逾期 = due_date 已过且未闭环），
    # 不再用 deviations.deadline 近似 —— 表语义与指标名对齐
    capa_open_count = (await db.execute(
        select(func.count()).select_from(Capa).where(Capa.status != "closed")
    )).scalar_one()
    capa_overdue_count = (await db.execute(
        select(func.count()).select_from(Capa).where(
            Capa.status != "closed",
            Capa.due_date.is_not(None),
            Capa.due_date < now,
        )
    )).scalar_one()
    overdue_rate = (capa_overdue_count / capa_open_count) if capa_open_count else 0.0

    # 逾期率趋势：本月新建 CAPA 的逾期率 vs 总体逾期率
    capa_open_this = (await db.execute(
        select(func.count()).select_from(Capa).where(
            Capa.status != "closed",
            Capa.created_at >= month_start,
        )
    )).scalar_one()
    capa_overdue_this = (await db.execute(
        select(func.count()).select_from(Capa).where(
            Capa.status != "closed",
            Capa.created_at >= month_start,
            Capa.due_date.is_not(None),
            Capa.due_date < now,
        )
    )).scalar_one()
    overdue_rate_this = (capa_overdue_this / capa_open_this) if capa_open_this else 0.0

    # 近 7 天未解决净变化：新发现 - 新结案
    new_open_7d = (await db.execute(
        select(func.count()).select_from(Deviation).where(
            Deviation.discovered_at >= week_ago,
        )
    )).scalar_one()
    closed_7d = (await db.execute(
        select(func.count()).select_from(Deviation).where(
            Deviation.status == "closed",
            func.coalesce(Deviation.closed_at, Deviation.updated_at) >= week_ago,
        )
    )).scalar_one()

    return KPIOut(
        unsolved_count=unsolved,
        major_count_this_month=major_count,
        avg_cycle_days=round(avg_cycle, 1),
        capa_overdue_rate=round(overdue_rate, 4),
        trend_unsolved_delta=int(new_open_7d - closed_7d),
        trend_major_delta=int(major_count - major_count_last_month),
        trend_cycle_delta=round(avg_cycle_30d - avg_cycle, 1),
        trend_capa_delta=round(overdue_rate_this - overdue_rate, 4),
    )


@router.get("/dashboard/trend", response_model=list[DeviationTrendPoint], summary="偏差趋势图数据")
@api_limit
async def get_trend(
    request: Request,
    days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(get_db_from_auth),
    _: AuthContext = Depends(require("case:read")),
) -> list[DeviationTrendPoint]:
    """获取近 N 天的偏差类型趋势"""
    rows = (await db.execute(
        select(
            func.to_char(Deviation.discovered_at, "YYYY-MM-DD").label("date"),
            Deviation.case_type,
            func.count().label("cnt"),
        )
        .where(Deviation.discovered_at >= func.now() - text(f"interval '{days} days'"))
        .group_by("date", Deviation.case_type)
        .order_by("date")
    )).all()

    # 聚合到 DeviationTrendPoint
    by_date = {}
    for r in rows:
        d = r.date
        if d not in by_date:
            by_date[d] = DeviationTrendPoint(date=d)
        pt = by_date[d]
        ct = (r.case_type or "").lower()
        if "microbe" in ct or "em" in ct:
            pt.microbe += r.cnt
        elif "particle" in ct:
            pt.particle += r.cnt
        elif "impurity" in ct:
            pt.impurity += r.cnt
        elif "sign" in ct:
            pt.signature += r.cnt
        elif "hvac" in ct:
            pt.hvac += r.cnt
        elif "clean" in ct:
            pt.cleaning += r.cnt

    return list(by_date.values())


# ============================================================
#  源系统注册表
# ============================================================
@router.get("/source-systems", response_model=list[SourceSystemOut], summary="源系统列表")
@api_limit
async def list_source_systems(
    request: Request,
    db: AsyncSession = Depends(get_db_from_auth),
    _: AuthContext = Depends(require("case:read")),
) -> list[SourceSystemOut]:
    from shared.models import SourceSystem
    rows = (await db.execute(select(SourceSystem).order_by(SourceSystem.code))).scalars().all()
    return [SourceSystemOut.model_validate(r) for r in rows]
