"""Case Service — ICH Q9 报告版本化管理

激活 models.Report 僵尸表：Drafter 报告的版本化快照、人工修订版本、QA 批准。
每个版本不可变（修订=新版本），case_id+version 唯一（DB 级约束 reports_case_version_uniq）。
"""
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.audit import append_audit_log
from shared.auth_middleware import AuthContext, get_db_from_auth, require
from shared.exceptions import ConflictError, NotFoundError, ValidationError
from shared.models import Deviation, Report
from shared.rate_limit import write_limit
from shared.schemas import ReportCreate, ReportOut

router = APIRouter(tags=["report"])

REPORT_STATUSES = ("draft", "under_review", "approved", "rejected")


async def _get_case_or_404(db: AsyncSession, case_id: str) -> Deviation:
    case = (await db.execute(select(Deviation).where(Deviation.id == case_id))).scalar_one_or_none()
    if case is None:
        raise NotFoundError("偏差案例不存在")
    return case


@router.get("/cases/{case_id}/reports", response_model=list[ReportOut])
async def list_reports(
    case_id: str,
    db: AsyncSession = Depends(get_db_from_auth),
    auth: AuthContext = Depends(require("case:read")),
) -> list[Report]:
    """列出案例全部报告版本（新→旧）"""
    await _get_case_or_404(db, case_id)
    result = await db.execute(
        select(Report).where(Report.case_id == case_id).order_by(Report.version.desc())
    )
    return list(result.scalars().all())


@router.get("/cases/{case_id}/reports/{version}", response_model=ReportOut)
async def get_report(
    case_id: str,
    version: int,
    db: AsyncSession = Depends(get_db_from_auth),
    auth: AuthContext = Depends(require("case:read")),
) -> Report:
    """读取指定版本报告"""
    await _get_case_or_404(db, case_id)
    report = (
        await db.execute(
            select(Report).where(Report.case_id == case_id, Report.version == version)
        )
    ).scalar_one_or_none()
    if report is None:
        raise NotFoundError(f"报告版本不存在: v{version}")
    return report


@router.post("/cases/{case_id}/reports", response_model=ReportOut, status_code=201)
@write_limit
async def create_report_version(
    request: Request,
    case_id: str,
    body: ReportCreate,
    db: AsyncSession = Depends(get_db_from_auth),
    auth: AuthContext = Depends(require("case:write")),
) -> Report:
    """创建下一版本报告（版本号自动 +1；结案后禁止新增版本）"""
    case = await _get_case_or_404(db, case_id)
    if case.status == "closed":
        raise ValidationError(
            "已结案的案例不可新增报告版本；如需修订请重新开案走受控变更流程",
            error_code="CASE_ALREADY_CLOSED",
        )
    next_version = int(
        (await db.execute(select(func.coalesce(func.max(Report.version), 0)).where(Report.case_id == case_id)))
        .scalar_one()
    ) + 1
    report = Report(
        id=f"RPT-{uuid4().hex[:12].upper()}",
        case_id=case_id,
        version=next_version,
        title=body.title,
        content=body.content,
        status="draft",
        author_id=auth.user_id,
        change_reason=body.change_reason,
    )
    db.add(report)
    try:
        await db.flush()
    except IntegrityError as exc:  # case_id+version 唯一约束并发冲突
        raise ConflictError("报告版本号并发冲突，请重试", error_code="REPORT_VERSION_CONFLICT") from exc
    await append_audit_log(
        db,
        operator=auth.email,
        action="REPORT_VERSION_CREATED",
        target=f"{case_id}/reports/v{next_version}",
        detail=f"创建报告 v{next_version}「{body.title}」"
        + (f" 变更原因: {body.change_reason}" if body.change_reason else ""),
    )
    await db.commit()
    await db.refresh(report)
    return report


@router.post("/cases/{case_id}/reports/{version}/approve", response_model=ReportOut)
@write_limit
async def approve_report(
    request: Request,
    case_id: str,
    version: int,
    db: AsyncSession = Depends(get_db_from_auth),
    auth: AuthContext = Depends(require("sign:approve")),
) -> Report:
    """QA 批准报告版本（draft/rejected → approved；批准人须非作者，四眼原则）"""
    report = (
        await db.execute(select(Report).where(Report.case_id == case_id, Report.version == version))
    ).scalar_one_or_none()
    if report is None:
        raise NotFoundError(f"报告版本不存在: v{version}")
    if report.status == "approved":
        raise ConflictError("该报告版本已批准", error_code="REPORT_ALREADY_APPROVED")
    if report.author_id == auth.user_id:
        raise ValidationError(
            "报告批准人须非作者本人（四眼原则）", error_code="REPORT_FOUR_EYES_REQUIRED"
        )
    report.status = "approved"
    report.approved_by = auth.user_id
    await append_audit_log(
        db,
        operator=auth.email,
        action="REPORT_APPROVED",
        target=f"{case_id}/reports/v{version}",
        detail=f"批准报告 v{version}（作者 {report.author_id}）",
    )
    await db.commit()
    await db.refresh(report)
    return report
