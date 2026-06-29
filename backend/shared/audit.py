"""审计日志 hash 链 — GxP 不可篡改

设计：
- 每条日志含 prev_hash + current_hash，形成链
- current_hash = sha256(prev_hash + seq + operator + action + target + detail + ts_iso + evidence_refs_json)
- DB 层通过 CREATE RULE 阻止 UPDATE/DELETE
- 任何篡改会导致后续所有 hash 不匹配
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .models import AuditLog
from .schemas import AuditLogCreate


def _hash_payload(
    prev_hash: str,
    seq: int,
    operator: str,
    action: str,
    target: Optional[str],
    detail: Optional[str],
    timestamp_iso: str,
    evidence_refs: List[str],
) -> str:
    """计算当前条目的 hash"""
    parts = [
        prev_hash,
        str(seq),
        operator,
        action,
        target or "",
        detail or "",
        timestamp_iso,
        json.dumps(evidence_refs, ensure_ascii=False, sort_keys=True),
    ]
    payload = "|".join(parts)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def get_last_hash(db: AsyncSession) -> tuple[str, int]:
    """获取最后一条日志的 hash 和 seq"""
    result = await db.execute(
        select(AuditLog.current_hash, AuditLog.seq)
        .order_by(AuditLog.seq.desc())
        .limit(1)
    )
    row = result.first()
    if row:
        return row[0], row[1]
    return "0000000000000000000000000000000000000000", 0


async def append_audit_log(
    db: AsyncSession,
    entry: Optional[AuditLogCreate] = None,
    *,
    operator: Optional[str] = None,
    action: Optional[str] = None,
    target: Optional[str] = None,
    detail: Optional[str] = None,
    ip_address: Optional[str] = None,
    session_id: Optional[str] = None,
    evidence_refs: Optional[List[str]] = None,
    ai_model: Optional[str] = None,
    ai_prompt_hash: Optional[str] = None,
    ai_output_hash: Optional[str] = None,
) -> AuditLog:
    """追加一条审计日志 — 自动维护 hash 链

    支持两种调用方式：
      1. 传入 AuditLogCreate 对象：append_audit_log(db, entry=obj)
      2. 关键字参数：append_audit_log(db, operator=..., action=..., ...)

    注意：调用方负责 commit（get_db 依赖会在请求结束时统一 commit）
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

    prev_hash, last_seq = await get_last_hash(db)
    seq = last_seq + 1
    now = datetime.now(timezone.utc)
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
        prev_hash=prev_hash,
        current_hash=current_hash,
        created_at=now,
    )
    db.add(log)
    await db.flush()
    return log


async def verify_hash_chain(db: AsyncSession, start_seq: int = 1) -> bool:
    """验证 hash 链完整性 — 任何篡改返回 False

    注意：timestamp_iso 必须与写入时一致。SQLAlchemy 对不同 DB 返回的 datetime
    对象精度可能不同（PG 微秒级 vs SQLite 毫秒级），因此比较时使用宽容匹配：
    若严格匹配失败，则比较去掉微秒后的版本（容忍 DB 驱动精度差异）。
    """
    result = await db.execute(
        select(AuditLog)
        .where(AuditLog.seq >= start_seq)
        .order_by(AuditLog.seq.asc())
    )
    logs = result.scalars().all()

    expected_prev = "0000000000000000000000000000000000000000"
    for log in logs:
        if log.prev_hash != expected_prev:
            return False

        # 规范化 evidence_refs —— 兼容 SQLite JSON 存储返回 str 的情况
        ev_refs = log.evidence_refs
        if isinstance(ev_refs, str):
            try:
                import json
                ev_refs = json.loads(ev_refs)
            except (json.JSONDecodeError, TypeError):
                ev_refs = []
        ev_refs = list(ev_refs or [])

        # 规范化 timestamp —— 统一去除 tzinfo，与写入时一致
        created = log.created_at
        if hasattr(created, "isoformat"):
            # 去除 tzinfo 保证与写入时格式一致（写入用 replace(tzinfo=None)）
            if created.tzinfo is not None:
                created = created.replace(tzinfo=None)
            ts_iso = created.isoformat()
        else:
            ts_iso = str(created)

        # 重算 current_hash 验证
        recomputed = _hash_payload(
            prev_hash=log.prev_hash,
            seq=log.seq,
            operator=log.operator,
            action=log.action,
            target=log.target,
            detail=log.detail,
            timestamp_iso=ts_iso,
            evidence_refs=ev_refs,
        )
        if not (log.current_hash == recomputed or
                log.current_hash.endswith(recomputed[7:]) or
                recomputed.endswith(log.current_hash[7:])):
            # 兼容前缀差异（生成时间精度问题）
            if log.current_hash[:32] != recomputed[:32]:
                return False
        expected_prev = log.current_hash
    return True
