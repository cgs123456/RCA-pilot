"""Case Service — CAPA（纠正预防措施）生命周期管理

激活 models.Capa 僵尸表：创建 → 评审 → 实施 → 有效性验证 → 闭环。
所有状态流转写审计（GxP 要求 CAPA 全程可追溯）。
"""
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.audit import append_audit_log
from shared.auth_middleware import AuthContext, get_db_from_auth, require
from shared.exceptions import ConflictError, NotFoundError, ValidationError
from shared.models import Capa, Deviation
from shared.notify import create_notification
from shared.rate_limit import write_limit
from shared.schemas import CapaCreate, CapaOut, CapaUpdate

router = APIRouter(tags=["capa"])

# CAPA 状态机：pending → under_review → implemented → verified → closed
# verified 是有效性检查通过的门槛（必须给出 effectiveness_rate），closed 只能从 verified 进入。
_CAPA_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"under_review"},
    "under_review": {"implemented", "pending"},  # pending = 评审退回
    "implemented": {"verified"},
    "verified": {"closed"},
    "closed": set(),
}


async def _get_case_or_404(db: AsyncSession, case_id: str) -> Deviation:
    case = (await db.execute(select(Deviation).where(Deviation.id == case_id))).scalar_one_or_none()
    if case is None:
        raise NotFoundError("偏差案例不存在")
    return case


@router.get("/cases/{case_id}/capas", response_model=list[CapaOut])
async def list_capas(
    case_id: str,
    db: AsyncSession = Depends(get_db_from_auth),
    auth: AuthContext = Depends(require("case:read")),
) -> list[Capa]:
    """列出案例的 CAPA（按创建时间升序）"""
    await _get_case_or_404(db, case_id)
    result = await db.execute(select(Capa).where(Capa.case_id == case_id).order_by(Capa.created_at))
    return list(result.scalars().all())


@router.post("/cases/{case_id}/capas", response_model=CapaOut, status_code=201)
@write_limit
async def create_capa(
    request: Request,
    case_id: str,
    body: CapaCreate,
    db: AsyncSession = Depends(get_db_from_auth),
    auth: AuthContext = Depends(require("case:write")),
) -> Capa:
    """创建 CAPA（案例结案后禁止新增，修订须重新开案）"""
    case = await _get_case_or_404(db, case_id)
    if case.status == "closed":
        raise ValidationError(
            "已结案的案例不可新增 CAPA；如需补充措施请重新开案走受控变更流程",
            error_code="CASE_ALREADY_CLOSED",
        )
    capa = Capa(
        id=f"CAPA-{uuid4().hex[:12].upper()}",
        case_id=case_id,
        title=body.title,
        description=body.description,
        severity=body.severity,
        status="pending",
        responsibility=body.responsibility,
        due_date=body.due_date,
    )
    db.add(capa)
    await db.flush()
    # P2-⑧ 通知案例调查员（创建人本人不打扰）
    if case.investigator_id and case.investigator_id != auth.user_id:
        await create_notification(
            db,
            case.investigator_id,
            "CAPA_CREATED",
            case_id=case_id,
            payload={"capa_id": capa.id, "title": body.title, "by": auth.email},
        )
    await append_audit_log(
        db,
        operator=auth.email,
        action="CAPA_CREATED",
        target=f"{case_id}/{capa.id}",
        detail=f"创建 CAPA「{body.title}」severity={body.severity} 责任人={body.responsibility}",
    )
    await db.commit()
    await db.refresh(capa)
    return capa


@router.patch("/capas/{capa_id}", response_model=CapaOut)
@write_limit
async def update_capa(
    request: Request,
    capa_id: str,
    body: CapaUpdate,
    db: AsyncSession = Depends(get_db_from_auth),
    auth: AuthContext = Depends(require("case:write")),
) -> Capa:
    """更新 CAPA 字段/状态流转（closed 为终态；转 verified 必须附有效性评分）"""
    capa = (await db.execute(select(Capa).where(Capa.id == capa_id))).scalar_one_or_none()
    if capa is None:
        raise NotFoundError("CAPA 不存在")
    case = await _get_case_or_404(db, capa.case_id)
    if case.status == "closed":
        raise ValidationError(
            "已结案的案例不可修改 CAPA；如需修订请重新开案走受控变更流程",
            error_code="CASE_ALREADY_CLOSED",
        )

    old_status = capa.status
    changes: list[str] = []
    if body.status is not None and body.status != old_status:
        if body.status not in _CAPA_TRANSITIONS.get(old_status, set()):
            raise ConflictError(
                f"CAPA 状态不允许从 {old_status} 流转到 {body.status}"
                f"（允许：{sorted(_CAPA_TRANSITIONS.get(old_status, set())) or '无，已终态'}）",
                error_code="CAPA_INVALID_TRANSITION",
            )
        if body.status == "verified" and body.effectiveness_rate is None and capa.effectiveness_rate is None:
            raise ValidationError(
                "CAPA 转入 verified 必须提供有效性评分 effectiveness_rate（0-100）",
                error_code="CAPA_EFFECTIVENESS_REQUIRED",
            )
        capa.status = body.status
        changes.append(f"status {old_status} → {body.status}")
        if body.status == "verified":
            capa.verified_by = auth.user_id
            capa.verified_at = datetime.now(UTC)
            changes.append(f"verified_by={auth.user_id}")

    if body.title is not None:
        capa.title = body.title
        changes.append("title")
    if body.description is not None:
        capa.description = body.description
        changes.append("description")
    if body.responsibility is not None:
        capa.responsibility = body.responsibility
        changes.append(f"responsibility={body.responsibility}")
    if body.due_date is not None:
        capa.due_date = body.due_date
        changes.append("due_date")
    if body.effectiveness_rate is not None:
        capa.effectiveness_rate = body.effectiveness_rate
        changes.append(f"effectiveness_rate={body.effectiveness_rate}")

    if not changes:
        raise ValidationError("未提供任何变更字段")

    await append_audit_log(
        db,
        operator=auth.email,
        action="CAPA_UPDATED",
        target=f"{capa.case_id}/{capa.id}",
        detail="CAPA 更新 — " + "; ".join(changes),
    )
    await db.commit()
    await db.refresh(capa)
    return capa
