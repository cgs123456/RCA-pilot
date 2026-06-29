"""Case Service — 偏差案例 CRUD + HITL 关卡 + Dashboard KPI"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shared.audit import append_audit_log
from shared.auth_middleware import AuthContext, get_auth_context, get_db_from_auth, require
from shared.database import get_db
from shared.exceptions import ForbiddenError, NotFoundError, ValidationError
from shared.rate_limit import api_limit, write_limit
from shared.models import (
    AgentRun,
    Deviation,
    Evidence,
    FiveWhy,
    SkepticChallenge,
    TimelineEvent,
    User,
)
from shared.schemas import (
    DeviationCreate,
    DeviationDetail,
    DeviationOut,
    DeviationTrendPoint,
    DeviationUpdate,
    HeatmapCell,
    HitlGateRequest,
    HitlGateResponse,
    KPIOut,
    Page,
    SignatureOut,
    SignatureRequest,
    SourceSystemOut,
)

router = APIRouter(tags=["case"])

# HITL 关卡定义
HITL_GATES = ["gate1", "gate2", "gate3", "gate4", "gate5"]
HITL_GATE_LABELS = {
    "gate1": "证据收集完整性确认",
    "gate2": "5 Whys 推理审查",
    "gate3": "Skeptic 反例审阅",
    "gate4": "CAPA 计划评审",
    "gate5": "报告最终签发",
}


# ============================================================
#  偏差 CRUD
# ============================================================
@router.get("/cases", response_model=Page[DeviationOut], summary="查询偏差案例列表")
@api_limit
async def list_cases(
    request: Request,
    status: Optional[str] = Query(default=None),
    severity: Optional[str] = Query(default=None),
    investigator_id: Optional[str] = Query(default=None),
    keyword: Optional[str] = Query(default=None, description="标题/ID 模糊搜索"),
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
    """创建偏差案例 — 自动生成 ID（若未提供）+ 初始化 HITL 关卡 + 审计日志"""
    # 生成 ID（格式 DEV-YYYY-MM-NNN）
    case_id = body.id
    if not case_id:
        now = datetime.now(timezone.utc)
        # 简单计数 — 生产中应使用 DB 序列
        count_stmt = select(func.count()).select_from(Deviation).where(
            func.to_char(Deviation.discovered_at, "YYYY-MM") == now.strftime("%Y-%m")
        )
        n = (await db.execute(count_stmt)).scalar_one() + 1
        case_id = f"DEV-{now.strftime('%Y-%m')}-{n:03d}"

    # 初始化 HITL 关卡
    hitl_gates = {g: "pending" for g in HITL_GATES}

    case = Deviation(
        id=case_id,
        title=body.title,
        description=body.description,
        severity=body.severity,
        status="pending",
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
        meta=body.meta,
    )
    db.add(case)
    try:
        await db.flush()
    except IntegrityError:
        raise ValidationError(f"案例 ID {case_id} 已存在")

    await append_audit_log(
        db,
        operator=auth.email,
        action="CREATE_DEVIATION",
        target=case_id,
        detail=f"创建偏差案例: {body.title}",
        ip_address=auth.ip_address,
        session_id=auth.session_id,
    )

    # 重新加载 case 并 eagerly load investigator 关系（避免 async lazy-load 报错）
    case = (
        await db.execute(
            select(Deviation)
            .options(selectinload(Deviation.investigator))
            .where(Deviation.id == case_id)
        )
    ).scalar_one()
    return DeviationOut.model_validate(case)


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
    - 此端点仅允许在 investigating 状态下更新业务字段（如 immediate_action/progress 等）
    """
    case = (
        await db.execute(
            select(Deviation)
            .options(selectinload(Deviation.investigator))
            .where(Deviation.id == case_id)
        )
    ).scalar_one_or_none()
    if not case:
        raise NotFoundError(f"案例 {case_id} 不存在")
    if case.status == "closed":
        raise ValidationError("已结案的案例不可修改")

    # 禁止通过 PATCH 直接设置 status=closed —— 必须走 /signatures 双签流程
    if body.status == "closed":
        raise ValidationError(
            "不允许通过 PATCH 直接结案；请通过 POST /cases/{case_id}/signatures "
            "（signature_meaning=approve）完成 QA 双签后系统自动结案"
        )

    # 禁止通过 PATCH 手动设置 reviewing —— 该状态仅由 HITL 5 关全通过触发
    if body.status == "reviewing":
        raise ValidationError(
            "reviewing 状态由系统在 HITL 5 关全部通过后自动设置，不可手动指定"
        )

    changes = []
    for field, value in body.model_dump(exclude_unset=True).items():
        if value is None:
            continue
        # status 字段只允许 pending → investigating 的正向流转
        if field == "status":
            if value not in ("investigating",):
                raise ValidationError(
                    f"PATCH 仅允许将 status 改为 investigating；其他状态变更请走专用端点"
                )
            if case.status not in ("pending",):
                raise ValidationError(
                    f"当前状态 {case.status} 不允许通过 PATCH 改为 {value}"
                )
        old = getattr(case, field)
        if old != value:
            changes.append(f"{field}: {old} → {value}")
            setattr(case, field, value)

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

    - gate_id 必须为 gate1~gate5
    - gate3 accept 会触发 Reasoner 重新推理（标记为 triggered_agent）
    - 全部 5 关通过 → 案例进入 reviewing 状态
    """
    if gate_id != body.gate_id:
        raise ValidationError("路径 gate_id 与请求体不一致")
    if gate_id not in HITL_GATES:
        raise ValidationError(f"无效的 gate_id，应为 {HITL_GATES}")

    # 加锁查询案例
    stmt = (
        select(Deviation)
        .options(selectinload(Deviation.investigator))
        .where(Deviation.id == case_id)
    )
    case = (await db.execute(stmt)).scalar_one_or_none()
    if not case:
        raise NotFoundError(f"案例 {case_id} 不存在")

    # 更新关卡状态
    hitl = dict(case.hitl_gates or {})
    hitl[gate_id] = "done" if body.action == "accept" else "rejected"
    case.hitl_gates = hitl

    # 推进进度
    progress_map = {"gate1": 20, "gate2": 40, "gate3": 60, "gate4": 80, "gate5": 100}
    if body.action == "accept":
        case.progress = max(case.progress, progress_map[gate_id])

    # 状态流转
    new_status = case.status
    all_passed = all(hitl.get(g) == "done" for g in HITL_GATES)
    if all_passed:
        case.status = "reviewing"
        new_status = "reviewing"
    elif gate_id == "gate1" and case.status == "pending":
        case.status = "investigating"
        new_status = "investigating"

    # gate3 accept → 触发 Reasoner 重新推理（Phase 2 实际调用 Agent Service）
    # gate3 reject → 若 hitl_gate3_auto_retry flag 启用，触发 Reasoner 重试（上限由 flag 配置）
    triggered_agent = None
    if gate_id == "gate3" and body.action == "accept":
        triggered_agent = "reasoner"
    elif gate_id == "gate3" and body.action == "reject":
        from shared.feature_flag import is_enabled, flag_context
        if is_enabled("hitl_gate3_auto_retry", user_id=auth.user_id):
            triggered_agent = "reasoner"
            retry_ctx = flag_context("hitl_gate3_auto_retry")
            # max_retries 由 flag 配置控制（默认 3）— 实际重试计数由 Agent Service 维护
            await append_audit_log(
                db,
                operator=auth.email,
                action="HITL_GATE3_AUTO_RETRY_TRIGGERED",
                target=f"{case_id}/gate3",
                detail=f"gate3 reject 触发 Reasoner 自动重试（上限 {retry_ctx.get('max_retries', 3)}）",
                ip_address=auth.ip_address,
                session_id=auth.session_id,
            )

    await append_audit_log(
        db,
        operator=auth.email,
        action=f"HITL_{body.action.upper()}",
        target=f"{case_id}/{gate_id}",
        detail=f"{HITL_GATE_LABELS[gate_id]} — {body.action} — {body.comment or ''}",
        ip_address=auth.ip_address,
        session_id=auth.session_id,
    )

    return HitlGateResponse(
        case_id=case_id,
        gate_id=gate_id,
        action=body.action,
        new_status=new_status,
        all_gates_passed=all_passed,
        triggered_agent=triggered_agent,
    )


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
    body: dict,
    db: AsyncSession = Depends(get_db_from_auth),
    auth: AuthContext = Depends(require("hitl:edit")),
) -> DeviationDetail:
    """更新 5 Whys 条目 — 用于 HITL gate2 人工编辑"""
    fw = (await db.execute(
        select(FiveWhy).where(FiveWhy.id == why_id, FiveWhy.case_id == case_id)
    )).scalar_one_or_none()
    if not fw:
        raise NotFoundError(f"5 Whys 条目 {why_id} 不存在")

    if "answer" in body:
        fw.answer = body["answer"]
    if "status" in body:
        fw.status = body["status"]
    if "is_root_cause" in body:
        fw.is_root_cause = bool(body["is_root_cause"])

    await append_audit_log(
        db,
        operator=auth.email,
        action="HITL_EDIT",
        target=f"{case_id}/five-why/{why_id}",
        detail=f"人工编辑 5 Whys #{fw.n}",
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
    body: dict,
    db: AsyncSession = Depends(get_db_from_auth),
    auth: AuthContext = Depends(require("hitl:accept")),
) -> HitlGateResponse:
    """处理 Skeptic 反例 — accept/reject + 解决方案"""
    sc = (await db.execute(
        select(SkepticChallenge).where(
            SkepticChallenge.id == challenge_id,
            SkepticChallenge.case_id == case_id,
        )
    )).scalar_one_or_none()
    if not sc:
        raise NotFoundError(f"Skeptic 挑战 {challenge_id} 不存在")

    action = body.get("action", "accept")
    sc.status = "accepted" if action == "accept" else "rejected"
    sc.resolution = body.get("resolution")
    sc.resolved_at = datetime.now(timezone.utc)

    await append_audit_log(
        db,
        operator=auth.email,
        action=f"SKEPTIC_{action.upper()}",
        target=f"{case_id}/{challenge_id}",
        detail=sc.resolution or "",
        ip_address=auth.ip_address,
        session_id=auth.session_id,
    )

    return HitlGateResponse(
        case_id=case_id,
        gate_id="gate3",
        action=action,
        new_status="investigating",
        all_gates_passed=False,
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

    - signature_meaning=submit: 调查员提交（要求 investigator 角色 + sign:submit 权限）
    - signature_meaning=approve: QA 批准（要求 qa_reviewer 角色 + sign:approve 权限）
    - 强制顺序：submit → approve（双人签名）
    - 二次密码确认
    """
    from shared.security import verify_password

    # 二次密码确认
    if not verify_password(body.password, auth.user.password_hash):
        raise ForbiddenError("密码确认失败")

    # 权限校验
    if body.signature_meaning == "submit":
        auth.check("sign:submit")
    elif body.signature_meaning == "approve":
        auth.check("sign:approve")

    case = (await db.execute(select(Deviation).where(Deviation.id == case_id))).scalar_one_or_none()
    if not case:
        raise NotFoundError(f"案例 {case_id} 不存在")

    sig_id = f"SIG-{case_id}-{body.signature_meaning.upper()}-{auth.user_id}"
    from shared.models import SignatureRecord
    sig = SignatureRecord(
        id=sig_id,
        case_id=case_id,
        signer_id=auth.user_id,
        signer_role=auth.user.role,
        signature_meaning="提交签名" if body.signature_meaning == "submit" else "批准签名",
        certificate_fingerprint=f"fp:{auth.user_id}:{int(datetime.now(timezone.utc).timestamp())}",
        ip_address=auth.ip_address,
        confirmation_text=body.confirmation_text,
    )
    db.add(sig)

    # 双签完成后结案
    if body.signature_meaning == "approve":
        case.status = "closed"
        case.progress = 100

    await append_audit_log(
        db,
        operator=auth.email,
        action=f"SIGN_{body.signature_meaning.upper()}",
        target=case_id,
        detail=f"{auth.user.name} ({auth.user.role}) 完成电子签名",
        ip_address=auth.ip_address,
        session_id=auth.session_id,
    )

    await db.flush()
    return SignatureOut.model_validate(sig)


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
    """计算 Dashboard 关键指标"""
    # 未解决案例数
    unsolved = (await db.execute(
        select(func.count()).select_from(Deviation).where(
            Deviation.status != "closed"
        )
    )).scalar_one()

    # 本月重大偏差数
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    major_count = (await db.execute(
        select(func.count()).select_from(Deviation).where(
            Deviation.severity == "major",
            Deviation.discovered_at >= month_start,
        )
    )).scalar_one()

    # 平均闭环天数
    closed_avg = (await db.execute(
        select(
            func.avg(
                func.extract("epoch", Deviation.updated_at - Deviation.discovered_at) / 86400
            )
        ).where(Deviation.status == "closed")
    )).scalar_one()
    avg_cycle = float(closed_avg or 0)

    return KPIOut(
        unsolved_count=unsolved,
        major_count_this_month=major_count,
        avg_cycle_days=round(avg_cycle, 1),
        capa_overdue_rate=0.0,
        trend_unsolved_delta=0,
        trend_major_delta=0,
        trend_cycle_delta=0,
        trend_capa_delta=0,
    )


@router.get("/dashboard/trend", response_model=List[DeviationTrendPoint], summary="偏差趋势图数据")
@api_limit
async def get_trend(
    request: Request,
    days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(get_db_from_auth),
    _: AuthContext = Depends(require("case:read")),
) -> List[DeviationTrendPoint]:
    """获取近 N 天的偏差类型趋势"""
    rows = (await db.execute(
        select(
            func.to_char(Deviation.discovered_at, "YYYY-MM-DD").label("date"),
            Deviation.case_type,
            func.count().label("cnt"),
        )
        .where(Deviation.discovered_at >= func.now() - func.cast(f"{days} days", func.text("interval")))
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
@router.get("/source-systems", response_model=List[SourceSystemOut], summary="源系统列表")
@api_limit
async def list_source_systems(
    request: Request,
    db: AsyncSession = Depends(get_db_from_auth),
    _: AuthContext = Depends(require("case:read")),
) -> List[SourceSystemOut]:
    from shared.models import SourceSystem
    rows = (await db.execute(select(SourceSystem).order_by(SourceSystem.code))).scalars().all()
    return [SourceSystemOut.model_validate(r) for r in rows]
