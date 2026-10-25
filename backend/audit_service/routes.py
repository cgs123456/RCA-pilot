"""Audit Service — 审计日志查询/验证/导出

注意：本文件不使用 `from __future__ import annotations`（PEP 563）——
slowapi 包装端点的字符串注解解析会落到 slowapi 模块命名空间导致 NameError。
"""
import csv
import io
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.audit import append_audit_log, verify_hash_chain
from shared.audit_anchor import anchor_chain_head, read_anchors, verify_against_anchors
from shared.auth_middleware import AuthContext, get_db_from_auth, require
from shared.models import AuditLog
from shared.rate_limit import api_limit, write_limit
from shared.schemas import AuditLogCreate, AuditLogOut, Page

router = APIRouter(prefix="/audit", tags=["audit"])

# 导出流式分窗大小
_EXPORT_CHUNK = 500

_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value: str | None) -> str:
    """CSV 公式注入防护 —— 以 =/+/-/@/Tab 开头的单元格前置单引号"""
    v = str(value or "")
    if v.startswith(_CSV_FORMULA_PREFIXES):
        return "'" + v
    return v


@router.get("/logs", response_model=Page[AuditLogOut], summary="查询审计日志")
@api_limit
async def list_logs(
    request: Request,
    case_id: str | None = Query(default=None, description="按案例 ID 过滤"),
    operator: str | None = Query(default=None, description="按操作员过滤"),
    action: str | None = Query(default=None, description="按动作过滤"),
    start: datetime | None = Query(default=None, description="起始时间 ISO8601"),
    end: datetime | None = Query(default=None, description="结束时间 ISO8601"),
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
    """验证整个 hash 链 — 任何篡改都会返回 valid=false（流式分窗校验，不整表加载）"""
    valid = await verify_hash_chain(db, start_seq=1)
    count = (
        await db.execute(
            select(AuditLog).with_only_columns(AuditLog.seq).order_by(desc(AuditLog.seq)).limit(1)
        )
    ).first()
    return {
        "valid": valid,
        "total_logs": count[0] if count else 0,
        "verified_at": datetime.now(UTC).isoformat(),
    }


@router.post("/logs", response_model=AuditLogOut, status_code=201, summary="写入审计日志（内部接口）")
@write_limit
async def write_log(
    request: Request,
    body: AuditLogCreate,
    db: AsyncSession = Depends(get_db_from_auth),
    auth: AuthContext = Depends(require("system:configure")),
) -> AuditLogOut:
    """写入一条审计日志 — 自动维护 hash 链

    GxP 可归因性约束：
    - 仅 admin（system:configure）可调用 —— 旧版 audit:read 让任何调查员都能伪造他人署名
    - operator 强制取当前认证用户；客户端声明值不一致时仅记入 detail 供追溯
    """
    declared = body.operator
    detail = body.detail or ""
    if declared and declared != auth.email:
        detail = f"[declared_operator={declared}（与认证身份不符，已强制归因到认证用户）] {detail}"

    log = await append_audit_log(
        db,
        operator=auth.email,  # 强制归因
        action=body.action,
        target=body.target,
        detail=detail,
        ip_address=body.ip_address or auth.ip_address,
        session_id=auth.session_id,
        evidence_refs=body.evidence_refs,
        ai_model=body.ai_model,
        ai_prompt_hash=body.ai_prompt_hash,
        ai_output_hash=body.ai_output_hash,
    )
    return AuditLogOut.model_validate(log)


def _export_filename(format: str) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"audit_export_{timestamp}.{format}"


async def _iter_logs(db: AsyncSession, *, case_id: str | None, start, end) -> AsyncIterator[AuditLog]:
    """按 seq 游标分窗流式读取审计日志（旧实现一次性加载全表到内存）"""
    cursor = 0
    while True:
        stmt = select(AuditLog).where(AuditLog.seq > cursor).order_by(AuditLog.seq).limit(_EXPORT_CHUNK)
        if case_id:
            stmt = stmt.where(AuditLog.target.like(f"{case_id}%"))
        if start:
            stmt = stmt.where(AuditLog.created_at >= start)
        if end:
            stmt = stmt.where(AuditLog.created_at <= end)
        rows = (await db.execute(stmt)).scalars().all()
        if not rows:
            return
        for r in rows:
            cursor = r.seq
            yield r
        if len(rows) < _EXPORT_CHUNK:
            return


@router.get("/export", summary="导出审计包（CSV/JSON）")
@api_limit
async def export_logs(
    request: Request,
    format: str = Query(default="csv", pattern="^(csv|json)$"),
    case_id: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    db: AsyncSession = Depends(get_db_from_auth),
    _: AuthContext = Depends(require("audit:export")),
) -> StreamingResponse:
    """导出审计日志包 — 用于 GxP 监管提交（流式生成，支持百万行级审计库）"""
    filename = _export_filename(format)

    if format == "csv":
        header = [
            "seq", "log_id", "timestamp", "operator", "action", "target",
            "detail", "ip_address", "session_id", "evidence_refs",
            "ai_model", "ai_prompt_hash", "ai_output_hash", "trace_id",
            "prev_hash", "current_hash",
        ]

        async def csv_gen() -> AsyncIterator[bytes]:
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(header)
            yield buf.getvalue().encode("utf-8")
            count = 0
            async for r in _iter_logs(db, case_id=case_id, start=start, end=end):
                buf.seek(0)
                buf.truncate(0)
                writer.writerow([
                    _csv_safe(str(r.seq)), _csv_safe(r.log_id), _csv_safe(r.created_at.isoformat()),
                    _csv_safe(r.operator), _csv_safe(r.action), _csv_safe(r.target),
                    _csv_safe(r.detail), _csv_safe(r.ip_address), _csv_safe(r.session_id),
                    _csv_safe("|".join(r.evidence_refs or [])),
                    _csv_safe(r.ai_model), _csv_safe(r.ai_prompt_hash), _csv_safe(r.ai_output_hash),
                    _csv_safe(r.trace_id),
                    _csv_safe(r.prev_hash), _csv_safe(r.current_hash),
                ])
                yield buf.getvalue().encode("utf-8")
                count += 1

        return StreamingResponse(
            csv_gen(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    async def json_gen() -> AsyncIterator[bytes]:
        yield b'{"exported_at": "' + filename.encode() + b'", "logs": ['
        first = True
        async for r in _iter_logs(db, case_id=case_id, start=start, end=end):
            item = AuditLogOut.model_validate(r).model_dump(mode="json")
            payload = json.dumps(item, ensure_ascii=False)
            yield (payload if first else "," + payload).encode("utf-8")
            first = False
        yield b']}'

    return StreamingResponse(
        json_gen(),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/anchor/records", summary="读取外部锚点记录（最近 N 条）")
@api_limit
async def list_anchor_records(
    request: Request,
    limit: int = Query(default=20, ge=1, le=500),
    _: AuthContext = Depends(require("audit:read")),
) -> dict:
    """读取审计链头外部锚点记录（WORM 文件尾部，新→旧）"""
    records = list(reversed(read_anchors(limit=limit)))
    return {"records": records, "count": len(records)}


@router.get("/anchor/verify", summary="比对库内链头与外部锚点（链尾删除/改写检测）")
@api_limit
async def verify_anchor_endpoint(
    request: Request,
    db: AsyncSession = Depends(get_db_from_auth),
    _: AuthContext = Depends(require("audit:read")),
) -> dict:
    """B9 篡改检测：外部锚点的 seq/hash 高于或不同于库内链头即证明审计库被删改"""
    return await verify_against_anchors(db)


@router.post("/anchor", summary="立即锚定当前链头到外部只读存储", status_code=201)
@write_limit
async def anchor_now(
    request: Request,
    db: AsyncSession = Depends(get_db_from_auth),
    auth: AuthContext = Depends(require("audit:export")),
) -> dict:
    """手动触发一次链头锚定（周期锚定由 audit-service 后台任务按配置间隔执行）。

    锚点只增不改 —— 锚定动作本身也写入审计链（ANCHOR_CREATED），
    锚点文件里因此出现自引用递进，删除锚点文件任意一行都会在下一次校验中暴露。
    """
    record = await anchor_chain_head(db)
    if record is None:
        return {"anchored": False, "note": "审计链为空，无锚点可写"}
    await append_audit_log(
        db,
        operator=auth.email,
        action="ANCHOR_CREATED",
        target=f"audit-anchor/seq/{record['seq']}",
        detail=f"链头 seq={record['seq']} 已锚定到外部只读存储",
        ip_address=auth.ip_address,
        session_id=auth.session_id,
    )
    return {"anchored": True, "record": record}


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
    _: AuthContext = Depends(require("system:configure")),
) -> dict:
    """热重载 feature flag 配置 —— 修改环境变量/配置文件后调用（admin only）"""
    from shared.feature_flag import reload_flags as _reload_flags
    _reload_flags()
    return {"status": "ok", "message": "feature flag 配置已重新加载"}
