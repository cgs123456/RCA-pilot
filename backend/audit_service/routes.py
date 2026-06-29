"""Audit Service — 审计日志查询/验证/导出"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.audit import append_audit_log, verify_hash_chain
from shared.auth_middleware import AuthContext, get_db_from_auth, require
from shared.models import AuditLog
from shared.rate_limit import api_limit, write_limit
from shared.schemas import AuditLogCreate, AuditLogOut, Page

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("/logs", response_model=Page[AuditLogOut], summary="查询审计日志")
@api_limit
async def list_logs(
    request: Request,
    case_id: Optional[str] = Query(default=None, description="按案例 ID 过滤"),
    operator: Optional[str] = Query(default=None, description="按操作员过滤"),
    action: Optional[str] = Query(default=None, description="按动作过滤"),
    start: Optional[datetime] = Query(default=None, description="起始时间 ISO8601"),
    end: Optional[datetime] = Query(default=None, description="结束时间 ISO8601"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db_from_auth),
    _: AuthContext = Depends(require("audit:read")),
) -> Page[AuditLogOut]:
    """查询审计日志 — 支持多维过滤 + 分页"""
    stmt = select(AuditLog)
    count_stmt = select(func.count()).select_from(AuditLog)

    if case_id:
        stmt = stmt.where(AuditLog.target.like(f"{case_id}%"))
        count_stmt = count_stmt.where(AuditLog.target.like(f"{case_id}%"))
    if operator:
        stmt = stmt.where(AuditLog.operator == operator)
        count_stmt = count_stmt.where(AuditLog.operator == operator)
    if action:
        stmt = stmt.where(AuditLog.action == action)
        count_stmt = count_stmt.where(AuditLog.action == action)
    if start:
        stmt = stmt.where(AuditLog.created_at >= start)
        count_stmt = count_stmt.where(AuditLog.created_at >= start)
    if end:
        stmt = stmt.where(AuditLog.created_at <= end)
        count_stmt = count_stmt.where(AuditLog.created_at <= end)

    # 使用 func.count() 在 DB 端聚合，避免取全行 ID 再 Python 端 len()
    total_count = (await db.execute(count_stmt)).scalar_one()

    stmt = stmt.order_by(desc(AuditLog.seq)).offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(stmt)).scalars().all()

    return Page[AuditLogOut](
        items=[AuditLogOut.model_validate(r) for r in rows],
        total=total_count,
        page=page,
        page_size=page_size,
    )


@router.get("/verify", summary="验证 hash 链完整性")
@api_limit
async def verify_chain(
    request: Request,
    db: AsyncSession = Depends(get_db_from_auth),
    _: AuthContext = Depends(require("audit:read")),
) -> dict:
    """验证整个 hash 链 — 任何篡改都会返回 valid=false"""
    valid = await verify_hash_chain(db, start_seq=1)
    count = (await db.execute(select(AuditLog).with_only_columns(AuditLog.seq).order_by(desc(AuditLog.seq)).limit(1))).first()
    return {
        "valid": valid,
        "total_logs": count[0] if count else 0,
        "verified_at": datetime.utcnow().isoformat(),
    }


@router.post("/logs", response_model=AuditLogOut, status_code=201, summary="写入审计日志（内部接口）")
@write_limit
async def write_log(
    request: Request,
    body: AuditLogCreate,
    db: AsyncSession = Depends(get_db_from_auth),
    auth: AuthContext = Depends(require("audit:read")),
) -> AuditLogOut:
    """写入一条审计日志 — 自动维护 hash 链

    注意：通常审计日志由其他服务在业务操作时通过 append_audit_log 直接写入。
    此端点用于测试和补充写入。
    """
    log = await append_audit_log(
        db,
        operator=body.operator,
        action=body.action,
        target=body.target,
        detail=body.detail,
        ip_address=body.ip_address or auth.ip_address,
        session_id=auth.session_id,
        evidence_refs=body.evidence_refs,
        ai_model=body.ai_model,
        ai_output_hash=body.ai_output_hash,
    )
    return AuditLogOut.model_validate(log)


@router.get("/export", summary="导出审计包（CSV/JSON）")
@api_limit
async def export_logs(
    request: Request,
    format: str = Query(default="csv", pattern="^(csv|json)$"),
    case_id: Optional[str] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    db: AsyncSession = Depends(get_db_from_auth),
    _: AuthContext = Depends(require("audit:export")),
) -> StreamingResponse:
    """导出审计日志包 — 用于 GxP 监管提交"""
    stmt = select(AuditLog).order_by(AuditLog.seq)
    if case_id:
        stmt = stmt.where(AuditLog.target.like(f"{case_id}%"))
    if start:
        stmt = stmt.where(AuditLog.created_at >= start)
    if end:
        stmt = stmt.where(AuditLog.created_at <= end)

    rows = (await db.execute(stmt)).scalars().all()

    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    filename = f"audit_export_{timestamp}.{'csv' if format == 'csv' else 'json'}"

    if format == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "seq", "log_id", "timestamp", "operator", "action", "target",
            "detail", "ip_address", "session_id", "evidence_refs",
            "ai_model", "ai_output_hash", "prev_hash", "current_hash",
        ])
        for r in rows:
            writer.writerow([
                r.seq, r.log_id, r.created_at.isoformat(), r.operator,
                r.action, r.target or "", r.detail or "",
                r.ip_address or "", r.session_id or "",
                "|".join(r.evidence_refs or []),
                r.ai_model or "", r.ai_output_hash or "",
                r.prev_hash, r.current_hash,
            ])
        content = buf.getvalue()
        media = "text/csv"
    else:
        items = [AuditLogOut.model_validate(r).model_dump(mode="json") for r in rows]
        content = json.dumps({
            "exported_at": timestamp,
            "total": len(items),
            "logs": items,
        }, ensure_ascii=False, indent=2)
        media = "application/json"

    return StreamingResponse(
        iter([content.encode("utf-8")]),
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ============================================================
#  Feature Flag 管理（运维端点）
# ============================================================
@router.get("/flags", summary="列出所有 feature flag 状态")
@api_limit
async def list_flags_endpoint(
    request: Request,
    _: AuthContext = Depends(require("audit:read")),
) -> dict:
    """列出所有 feature flag 及其当前配置 —— 用于运维查看灰度状态"""
    from shared.feature_flag import list_flags as _list_flags
    return {"flags": _list_flags()}


@router.post("/flags/reload", summary="热重载 feature flag 配置")
@api_limit
async def reload_flags_endpoint(
    request: Request,
    _: AuthContext = Depends(require("audit:read")),
) -> dict:
    """热重载 feature flag 配置 —— 修改环境变量/配置文件后调用"""
    from shared.feature_flag import reload_flags as _reload_flags
    _reload_flags()
    return {"status": "ok", "message": "feature flag 配置已重新加载"}
