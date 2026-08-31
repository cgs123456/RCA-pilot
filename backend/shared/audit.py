"""审计日志 hash 链 — GxP 不可篡改

设计：
- 每条日志含 prev_hash + current_hash，形成链
- current_hash 覆盖全部业务字段（含 ip/session/ai_* /trace_id），任何字段被改都断链
- seq 分配通过 PostgreSQL advisory lock 串行化，避免并发下重复 seq / 链分叉
- DB 层通过触发器阻止 UPDATE/DELETE/TRUNCATE（01_schema.sql + alembic 迁移）
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .models import AuditLog
from .schemas import AuditLogCreate

# 链初始 hash —— sha256 十六进制长度（64 字符）
GENESIS_HASH = "0" * 64

# 审计链写入串行锁（pg_advisory_xact_lock 的锁键）
_AUDIT_LOCK_KEY = "rca_audit_log_seq"

# hash 覆盖的字段版本号 —— 字段集变化时递增（旧链需重建）
_HASH_SCHEMA_VERSION = 2


def _normalize_evidence_refs(ev_refs: object) -> list[str]:
    """兼容 SQLite JSON 存储返回 str 的情况"""
    if isinstance(ev_refs, str):
        try:
            raw = json.loads(ev_refs)
        except (json.JSONDecodeError, TypeError):
            raw = []
        ev_refs = raw
    if isinstance(ev_refs, list):
        return [str(x) for x in ev_refs]
    return []


def _normalize_ts(created: object) -> str:
    """规范化时间戳 —— 与写入时（replace(tzinfo=None).isoformat()）一致"""
    if isinstance(created, datetime):
        if created.tzinfo is not None:
            created = created.replace(tzinfo=None)
        return created.isoformat()
    return str(created)


def _hash_payload(
    prev_hash: str,
    seq: int,
    operator: str,
    action: str,
    target: str | None,
    detail: str | None,
    timestamp_iso: str,
    evidence_refs: list[str],
    *,
    ip_address: str | None = None,
    session_id: str | None = None,
    ai_model: str | None = None,
    ai_prompt_hash: str | None = None,
    ai_output_hash: str | None = None,
    trace_id: str | None = None,
) -> str:
    """计算当前条目的 hash —— 覆盖全部审计字段

    v2：纳入 ip_address / session_id / ai_* / trace_id（v1 遗漏导致这些字段可被
    绕过链校验篡改）。字段集由 _HASH_SCHEMA_VERSION 标记。
    """
    parts = [
        f"v{_HASH_SCHEMA_VERSION}",
        prev_hash,
        str(seq),
        operator,
        action,
        target or "",
        detail or "",
        timestamp_iso,
        json.dumps(evidence_refs, ensure_ascii=False, sort_keys=True),
        ip_address or "",
        session_id or "",
        ai_model or "",
        ai_prompt_hash or "",
        ai_output_hash or "",
        trace_id or "",
    ]
    payload = "|".join(parts)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def _acquire_audit_lock(db: AsyncSession) -> None:
    """PostgreSQL 下取事务级 advisory lock，串行化 seq 分配。

    SQLite（测试）无此语句，直接跳过 —— SQLite 本身单写者。
    """
    try:
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": _AUDIT_LOCK_KEY})
    except Exception:
        # 锁失败不阻塞业务 —— 仍有 seq UNIQUE 兜底（fail-closed）
        pass


async def get_last_hash(db: AsyncSession) -> tuple[str, int]:
    """获取最后一条日志的 hash 和 seq（调用前应先取 advisory lock）"""
    result = await db.execute(
        select(AuditLog.current_hash, AuditLog.seq)
        .order_by(AuditLog.seq.desc())
        .limit(1)
    )
    row = result.first()
    if row:
        return row[0], row[1]
    return GENESIS_HASH, 0


async def append_audit_log(
    db: AsyncSession,
    entry: AuditLogCreate | None = None,
    *,
    operator: str | None = None,
    action: str | None = None,
    target: str | None = None,
    detail: str | None = None,
    ip_address: str | None = None,
    session_id: str | None = None,
    evidence_refs: list[str] | None = None,
    ai_model: str | None = None,
    ai_prompt_hash: str | None = None,
    ai_output_hash: str | None = None,
    trace_id: str | None = None,
) -> AuditLog:
    """追加一条审计日志 — 自动维护 hash 链

    支持两种调用方式：
      1. 传入 AuditLogCreate 对象：append_audit_log(db, entry=obj)
      2. 关键字参数：append_audit_log(db, operator=..., action=..., ...)

    注意：调用方负责 commit（get_db 依赖会在请求结束时统一 commit）。
    """
    if entry is None:
        if operator is None or action is None:
            raise ValueError("append_audit_log 需要传入 entry 或 operator+action")
        entry = AuditLogCreate(
            operator=operator,
            action=action,
            target=target,
            detail=detail,
            ip_address=ip_address,
            session_id=session_id,
            evidence_refs=evidence_refs or [],
            ai_model=ai_model,
            ai_prompt_hash=ai_prompt_hash,
            ai_output_hash=ai_output_hash,
        )
    else:
        # 同时传入 entry 和 session_id 时，session_id 覆盖 entry.session_id
        if session_id is not None:
            entry = entry.model_copy(update={"session_id": session_id})

    if trace_id is None:
        # trace_id 未显式传入时，自动带当前请求的 traceId（贯穿排障）
        from .error_handler import get_trace_id
        trace_id = get_trace_id() or None

    # 并发安全：先取 advisory lock 再读链尾，避免两个请求读到同一 prev_hash/seq
    await _acquire_audit_lock(db)

    prev_hash, last_seq = await get_last_hash(db)
    seq = last_seq + 1
    now = datetime.now(UTC)
    # 使用不含 tz 后缀的 ISO 格式，避免不同 DB 驱动对 tzinfo 处理不一致
    # （SQLite 读出 datetime 会丢失 tzinfo，PostgreSQL 保留；统一用无 tz 格式保证 hash 稳定）
    timestamp_iso = now.replace(tzinfo=None).isoformat()

    current_hash = _hash_payload(
        prev_hash=prev_hash,
        seq=seq,
        operator=entry.operator,
        action=entry.action,
        target=entry.target,
        detail=entry.detail,
        timestamp_iso=timestamp_iso,
        evidence_refs=entry.evidence_refs,
        ip_address=entry.ip_address,
        session_id=entry.session_id,
        ai_model=entry.ai_model,
        ai_prompt_hash=entry.ai_prompt_hash,
        ai_output_hash=entry.ai_output_hash,
        trace_id=trace_id,
    )

    log_id = (
        f"LOG-{now.strftime('%Y%m%dT%H%M%S')}"
        f"-{seq:04d}"
    )

    log = AuditLog(
        log_id=log_id,
        seq=seq,
        operator=entry.operator,
        action=entry.action,
        target=entry.target,
        detail=entry.detail,
        ip_address=entry.ip_address,
        session_id=entry.session_id,
        evidence_refs=entry.evidence_refs,
        ai_model=entry.ai_model,
        ai_prompt_hash=entry.ai_prompt_hash,
        ai_output_hash=entry.ai_output_hash,
        trace_id=trace_id,
        prev_hash=prev_hash,
        current_hash=current_hash,
        created_at=now,
    )
    db.add(log)
    await db.flush()
    return log


async def verify_hash_chain(db: AsyncSession, start_seq: int = 1) -> bool:
    """验证 hash 链完整性 — 任何篡改返回 False

    流式分窗校验（每窗 1000 条），不把整表载入内存 —— 审计日志按 GxP 要求
    保留 10 年，百万行级全表加载会拖垮 verify 端点。
    """
    from .models import AuditLog  # 局部导入避免循环

    expected_prev: str | None = None
    cursor = start_seq
    chunk_size = 1000

    while True:
        result = await db.execute(
            select(AuditLog)
            .where(AuditLog.seq >= cursor)
            .order_by(AuditLog.seq.asc())
            .limit(chunk_size)
        )
        logs = result.scalars().all()
        if not logs:
            break

        for log in logs:
            if expected_prev is None:
                # 第一条：链头必须从 GENESIS 开始
                if log.seq != start_seq and start_seq != 1:
                    # 从中间开始校验时信任起点 prev（只验证窗口内部链）
                    pass
                elif log.prev_hash != GENESIS_HASH:
                    return False
                expected_prev = log.prev_hash
            if log.prev_hash != expected_prev:
                return False

            recomputed = _hash_payload(
                prev_hash=log.prev_hash,
                seq=log.seq,
                operator=log.operator,
                action=log.action,
                target=log.target,
                detail=log.detail,
                timestamp_iso=_normalize_ts(log.created_at),
                evidence_refs=_normalize_evidence_refs(log.evidence_refs),
                ip_address=log.ip_address,
                session_id=log.session_id,
                ai_model=log.ai_model,
                ai_prompt_hash=log.ai_prompt_hash,
                ai_output_hash=log.ai_output_hash,
                trace_id=log.trace_id,
            )
            # 精确匹配 —— 不再宽容（旧版"前 32 位匹配"把完整性校验降级成 128bit 前缀比较）
            if log.current_hash != recomputed:
                return False
            expected_prev = log.current_hash

        cursor = logs[-1].seq + 1
        if len(logs) < chunk_size:
            break

    return True
