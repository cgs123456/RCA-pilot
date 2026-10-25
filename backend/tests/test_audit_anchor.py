"""B9 审计链外部锚定测试 — 锚点写入/读取/篡改检测

覆盖：
- 空链不写锚点；正常链头追加 JSONL 记录
- 追加式锚定单调递增（prev_seq 链接前后锚点）
- 损坏行跳过、文件缺失返回空
- verify_against_anchors：链尾删除/链改写/一致 三分支
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

import shared.audit_anchor as audit_anchor
from shared.audit import append_audit_log
from shared.audit_anchor import (
    anchor_chain_head,
    read_anchors,
    verify_against_anchors,
)
from shared.models import AuditLog

pytestmark = pytest.mark.asyncio


@pytest.fixture
def anchor_file(tmp_path, monkeypatch):
    """把锚点文件指到测试临时目录"""
    path = tmp_path / "anchor.jsonl"
    monkeypatch.setattr(audit_anchor.settings, "audit_anchor_path", str(path))
    return path


async def _seed(db: AsyncSession, n: int = 1) -> None:
    for i in range(n):
        await append_audit_log(db, operator="tester", action=f"ACT-{i}", target="DEV-001")
    await db.commit()


class TestAnchorChainHead:
    async def test_empty_chain_writes_nothing(self, test_db, anchor_file):
        assert await anchor_chain_head(test_db) is None
        assert not anchor_file.exists()

    async def test_anchor_writes_jsonl_record(self, test_db, anchor_file):
        await _seed(test_db, n=2)
        record = await anchor_chain_head(test_db)
        assert record is not None
        assert record["seq"] == 2
        assert record["hash"].startswith("sha256:")
        assert record["anchor_version"] == 1
        lines = anchor_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["seq"] == 2

    async def test_anchor_is_append_only_monotonic(self, test_db, anchor_file):
        await _seed(test_db, n=1)
        r1 = await anchor_chain_head(test_db)
        assert r1 is not None
        await append_audit_log(test_db, operator="tester", action="ACT-2", target="DEV-001")
        await test_db.commit()
        r2 = await anchor_chain_head(test_db)
        assert r2 is not None
        assert r2["seq"] > r1["seq"]
        assert r2["prev_seq"] == r1["seq"]
        assert len(read_anchors()) == 2

    async def test_read_anchors_missing_file_returns_empty(self, anchor_file):
        assert read_anchors() == []

    async def test_read_anchors_skips_corrupt_line(self, test_db, anchor_file):
        await _seed(test_db, n=1)
        await anchor_chain_head(test_db)
        with open(anchor_file, "a", encoding="utf-8") as f:
            f.write("{corrupt-not-json\n")
        await append_audit_log(test_db, operator="tester", action="ACT-2", target="DEV-001")
        await test_db.commit()
        await anchor_chain_head(test_db)
        anchors = read_anchors()
        assert len(anchors) == 2
        assert anchors[0]["seq"] == 1 and anchors[1]["seq"] == 2


class TestVerifyAgainstAnchors:
    async def test_no_anchor_reported_as_unanchored_valid(self, test_db, anchor_file):
        await _seed(test_db)
        result = await verify_against_anchors(test_db)
        assert result == {"anchored": False, "valid": True, "note": "尚无外部锚点"}

    async def test_consistent_head_is_valid(self, test_db, anchor_file):
        await _seed(test_db)
        await anchor_chain_head(test_db)
        result = await verify_against_anchors(test_db)
        assert result["anchored"] is True
        assert result["valid"] is True

    async def test_tail_deletion_detected(self, test_db, anchor_file):
        """锚定 seq=3 后删除 seq>1 的行 —— 库内链头落后于锚点即篡改"""
        await _seed(test_db, n=3)
        await anchor_chain_head(test_db)
        await test_db.execute(delete(AuditLog).where(AuditLog.seq > 1))
        await test_db.commit()
        result = await verify_against_anchors(test_db)
        assert result["valid"] is False
        assert "链尾删除" in result["detail"]

    async def test_hash_rewrite_detected(self, test_db, anchor_file):
        """同 seq 改写内容并重算链头 hash（模拟重放攻击）→ 与锚点不一致"""
        await _seed(test_db, n=1)
        await anchor_chain_head(test_db)
        await test_db.execute(
            update(AuditLog).values(detail="篡改后的详情", current_hash="sha256:" + "f" * 60)
        )
        await test_db.commit()
        result = await verify_against_anchors(test_db)
        assert result["valid"] is False
        assert "改写" in result["detail"]


class TestSeqConflictRetry:
    async def test_seq_conflict_retries_and_survives(self, test_db, monkeypatch):
        """B10：seq UNIQUE 冲突 → SAVEPOINT 回滚 → 重读链尾重试成功，业务事务存活"""
        from shared import audit as audit_mod

        # 先占住 seq=1（模拟另一写入者已落链）
        await _seed(test_db, n=1)
        await test_db.commit()

        real_get_last_hash = audit_mod.get_last_hash
        calls = {"n": 0}

        async def stale_first(db):
            calls["n"] += 1
            if calls["n"] == 1:
                # 首轮返回过期链尾（空链）→ 分配出被占用的 seq=1 → 唯一冲突
                return audit_mod.GENESIS_HASH, 0
            return await real_get_last_hash(db)

        monkeypatch.setattr(audit_mod, "get_last_hash", stale_first)

        log = await audit_mod.append_audit_log(
            test_db, operator="tester", action="ACT-RETRY", target="DEV-001"
        )
        assert log.seq == 2
        await test_db.commit()

        # 业务事务仍可用：继续写正常（无残留 savepoint/污染）
        log2 = await audit_mod.append_audit_log(
            test_db, operator="tester", action="ACT-AFTER", target="DEV-001"
        )
        await test_db.commit()
        assert log2.seq == 3
        assert await audit_mod.verify_hash_chain(test_db) is True
        assert calls["n"] >= 2  # 确实走了重试路径
