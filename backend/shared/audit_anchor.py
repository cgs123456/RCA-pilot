"""审计链外部锚定（B9）— 把链头 (seq, current_hash) 周期锚定到外部只读存储

hash 链只能证明"链内自洽"：删除链尾 N 条后 verify 仍通过（校验起点之后的链是
完整的）。把链头定期写入具有 WORM 属性的外部存储，校验时比对"外部锚点记录的
最高 seq/hash"与库内链头——库内 seq 落后于锚点即证明链尾被删除，seq 相同但
hash 不同即证明链被改写。

本地实现为 O_APPEND 追加文件（deploy 挂载为卷，配合宿主侧备份即可满足
不可篡改留存）；生产可把 audit_anchor_path 指向对象锁存储的同步落盘路径。
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings


def anchor_file_path() -> str:
    return settings.audit_anchor_path


async def read_chain_head(db: AsyncSession) -> tuple[int, str]:
    """返回当前链头 (seq, current_hash)；空链返回 (0, "")"""
    from .models import AuditLog  # 局部导入避免循环

    row = (
        await db.execute(
            select(AuditLog.seq, AuditLog.current_hash).order_by(AuditLog.seq.desc()).limit(1)
        )
    ).first()
    return (int(row[0]), str(row[1])) if row else (0, "")


def read_anchors(limit: int = 100) -> list[dict[str, Any]]:
    """读取最近 limit 条锚点记录（文件不存在返回空列表）

    文件只增不改：损坏行跳过但保留在文件里供运维排查。
    """
    path = anchor_file_path()
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines[-max(0, limit):]:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def last_anchor() -> dict[str, Any] | None:
    anchors = read_anchors(limit=1)
    return anchors[-1] if anchors else None


async def anchor_chain_head(db: AsyncSession) -> dict[str, Any] | None:
    """把当前链头追加到锚点文件。返回锚点记录；空链返回 None

    单行 O_APPEND 追加写 —— 进程崩溃最多丢失正在写的最后一行，不影响已落盘锚点。
    audit-service 多 worker（UVICORN_WORKERS>1）时每个 worker 都跑锚定循环，
    PG 下用事务级 advisory lock（741122）保证同一时刻只有一个 worker 追加，
    锁随事务提交自动释放；SQLite（测试）跳过。
    """
    from sqlalchemy import text

    if db.get_bind().dialect.name == "postgresql":
        got_lock = (
            await db.execute(text("SELECT pg_try_advisory_xact_lock(741122)"))
        ).scalar()
        if not got_lock:
            return None

    seq, head_hash = await read_chain_head(db)
    if seq == 0:
        return None
    prev = last_anchor()
    record: dict[str, Any] = {
        "anchored_at": datetime.now(UTC).isoformat(),
        "seq": seq,
        "hash": head_hash,
        "prev_seq": prev.get("seq") if prev else None,
        "anchor_version": 1,
    }
    path = anchor_file_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return record


async def verify_against_anchors(db: AsyncSession) -> dict[str, Any]:
    """比对库内链头与最后一个外部锚点（B9 篡改检测）

    - 无锚点 → anchored=False（不判失败：锚定是新增能力，历史数据无锚点属正常）
    - 锚点 seq > 库内 seq → 链尾被删除
    - seq 相同但 hash 不同 → 链被改写
    """
    seq, head_hash = await read_chain_head(db)
    anchors = read_anchors(limit=1)
    if not anchors:
        return {"anchored": False, "valid": True, "note": "尚无外部锚点"}
    a = anchors[-1]
    a_seq = int(a.get("seq", 0))
    a_hash = str(a.get("hash", ""))
    if seq < a_seq:
        return {
            "anchored": True,
            "valid": False,
            "detail": f"审计库链头 seq={seq} 落后于外部锚点 seq={a_seq}，检测到链尾删除",
            "anchor_seq": a_seq,
            "db_seq": seq,
        }
    if seq == a_seq and head_hash != a_hash:
        return {
            "anchored": True,
            "valid": False,
            "detail": f"seq={seq} 的链头 hash 与外部锚点不一致，检测到链改写",
            "anchor_seq": a_seq,
            "db_seq": seq,
        }
    return {"anchored": True, "valid": True, "anchor_seq": a_seq, "db_seq": seq}
