"""Evidence Service — 证据查询 + 时间线 + Connector 编排"""
from __future__ import annotations

from typing import List, Optional

import httpx
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth_middleware import AuthContext, get_db_from_auth, require
from shared.config import settings
from shared.exceptions import ConnectorError, NotFoundError
from shared.models import Evidence, TimelineEvent
from shared.rate_limit import api_limit, write_limit
from shared.schemas import EvidenceOut, Page, TimelineEventOut

router = APIRouter(tags=["evidence"])


@router.get("/evidence", response_model=Page[EvidenceOut], summary="查询证据列表")
@api_limit
async def list_evidence(
    request: Request,
    case_id: Optional[str] = Query(default=None),
    source_system: Optional[str] = Query(default=None, description="LIMS|MES|EMS|CMMS|LMS|BMS"),
    min_confidence: Optional[int] = Query(default=None, ge=1, le=4),
    keyword: Optional[str] = Query(default=None, description="摘要模糊搜索"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db_from_auth),
    _: AuthContext = Depends(require("evidence:read")),
) -> Page[EvidenceOut]:
    """查询证据列表 — 支持多维过滤"""
    stmt = select(Evidence)
    if case_id:
        stmt = stmt.where(Evidence.case_id == case_id)
    if source_system:
        stmt = stmt.where(Evidence.source_system == source_system)
    if min_confidence:
        stmt = stmt.where(Evidence.confidence >= min_confidence)
    if keyword:
        stmt = stmt.where(Evidence.summary.ilike(f"%{keyword}%"))

    # count — 使用 func.count() 而非取全行
    from sqlalchemy import func
    count_stmt = select(func.count()).select_from(Evidence)
    if case_id:
        count_stmt = count_stmt.where(Evidence.case_id == case_id)
    if source_system:
        count_stmt = count_stmt.where(Evidence.source_system == source_system)
    if min_confidence:
        count_stmt = count_stmt.where(Evidence.confidence >= min_confidence)
    if keyword:
        count_stmt = count_stmt.where(Evidence.summary.ilike(f"%{keyword}%"))
    total = (await db.execute(count_stmt)).scalar_one()

    stmt = stmt.order_by(desc(Evidence.collected_at)).offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(stmt)).scalars().all()

    return Page[EvidenceOut](
        items=[EvidenceOut.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/evidence/{evidence_id}", response_model=EvidenceOut, summary="查询证据详情")
@api_limit
async def get_evidence(
    request: Request,
    evidence_id: str,
    db: AsyncSession = Depends(get_db_from_auth),
    _: AuthContext = Depends(require("evidence:read")),
) -> EvidenceOut:
    """查询证据完整信息（含 raw_data）"""
    ev = (await db.execute(select(Evidence).where(Evidence.id == evidence_id))).scalar_one_or_none()
    if not ev:
        raise NotFoundError(f"证据 {evidence_id} 不存在")
    return EvidenceOut.model_validate(ev)


@router.get("/cases/{case_id}/timeline", response_model=List[TimelineEventOut], summary="案例时间线")
@api_limit
async def get_timeline(
    request: Request,
    case_id: str,
    db: AsyncSession = Depends(get_db_from_auth),
    _: AuthContext = Depends(require("evidence:read")),
) -> List[TimelineEventOut]:
    """获取案例的时间线事件 — 按时间排序"""
    rows = (await db.execute(
        select(TimelineEvent)
        .where(TimelineEvent.case_id == case_id)
        .order_by(TimelineEvent.occurred_at, TimelineEvent.sort_order)
    )).scalars().all()
    return [TimelineEventOut.model_validate(r) for r in rows]


@router.post("/evidence/fetch-from-source", summary="从源系统拉取增量证据（触发 Connector）")
@write_limit
async def fetch_from_source(
    request: Request,
    source: str = Query(..., description="源系统代码，Phase 1 仅支持 LMS"),
    case_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db_from_auth),
    auth: AuthContext = Depends(require("evidence:write")),
) -> dict:
    """从源系统 Connector 拉取最新证据 — Phase 1 仅对接 LMS Mock"""
    if source not in ("LMS", "LIMS", "MES", "EMS", "CMMS", "BMS"):
        raise ConnectorError(f"不支持的源系统: {source}")

    connector_map = {
        "LMS": settings.lms_connector_url,
    }
    url = connector_map.get(source)
    if not url:
        # Phase 1 仅 LMS Connector 可用，其他源系统使用已有静态数据
        return {
            "status": "skipped",
            "source": source,
            "message": f"{source} Connector 暂未实现，请使用已有证据数据",
            "fetched_count": 0,
        }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{url}/lms/cert-status",
                params={"employee_id": "EMP-2187"} if case_id else {},
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        raise ConnectorError(f"LMS Connector 调用失败: {e}")

    return {
        "status": "ok",
        "source": source,
        "data": data,
        "fetched_count": len(data) if isinstance(data, list) else 1,
        "triggered_by": auth.email,
    }
