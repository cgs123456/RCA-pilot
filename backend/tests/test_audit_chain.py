"""审计日志 hash 链测试 — 完整性 + 篡改检测

覆盖：
- 连续 append 后 hash 链前后衔接
- verify_hash_chain 对完整链返回 True
- 篡改任意字段后 verify 返回 False
- _hash_payload 确定性（相同输入相同输出）
- evidence_refs 顺序敏感
"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from shared.audit import (
    _hash_payload,
    append_audit_log,
    get_last_hash,
    verify_hash_chain,
)
from shared.schemas import AuditLogCreate

pytestmark = pytest.mark.asyncio


class TestHashPayload:
    """_hash_payload 单元测试"""

    def test_deterministic_same_input(self):
        """相同输入应产生相同 hash"""
        args = {
            "prev_hash": "0" * 64,
            "seq": 1,
            "operator": "tester",
            "action": "TEST",
            "target": "case-1",
            "detail": "测试详情",
            "timestamp_iso": "2025-08-17T06:00:00+00:00",
            "evidence_refs": ["E-001", "E-002"],
        }
        h1 = _hash_payload(**args)
        h2 = _hash_payload(**args)
        assert h1 == h2
        assert h1.startswith("sha256:")

    def test_different_seq_different_hash(self):
        """seq 变化 → hash 变化"""
        h1 = _hash_payload("0" * 64, 1, "u", "ACT", None, None, "ts", [])
        h2 = _hash_payload("0" * 64, 2, "u", "ACT", None, None, "ts", [])
        assert h1 != h2

    def test_evidence_refs_order_sensitive(self):
        """evidence_refs 顺序敏感（sort_keys 后 list 顺序固定）"""
        h1 = _hash_payload("0" * 64, 1, "u", "ACT", None, None, "ts", ["A", "B"])
        h2 = _hash_payload("0" * 64, 1, "u", "ACT", None, None, "ts", ["B", "A"])
        assert h1 != h2, "evidence_refs 顺序应影响 hash"

    def test_none_target_treated_as_empty(self):
        """target=None 等同 target=''"""
        h1 = _hash_payload("0" * 64, 1, "u", "ACT", None, None, "ts", [])
        h2 = _hash_payload("0" * 64, 1, "u", "ACT", "", None, "ts", [])
        h3 = _hash_payload("0" * 64, 1, "u", "ACT", "", "", "ts", [])
        assert h1 == h2 == h3


class TestAppendAuditLog:
    """append_audit_log 集成测试"""

    async def test_append_single_log(self, test_db: AsyncSession):
        """追加单条日志后 get_last_hash 返回正确值"""
        log = await append_audit_log(
            test_db,
            operator="tester",
            action="CREATE",
            target="DEV-001",
            detail="创建测试",
        )
        await test_db.commit()

        assert log.seq == 1
        assert log.prev_hash == "0" * 64
        assert log.current_hash.startswith("sha256:")
        assert log.log_id.startswith("LOG-")

        last_hash, last_seq = await get_last_hash(test_db)
        assert last_hash == log.current_hash
        assert last_seq == 1

    async def test_append_multiple_logs_chain(self, test_db: AsyncSession):
        """连续追加多条日志，prev_hash 应衔接"""
        log1 = await append_audit_log(
            test_db, operator="u1", action="ACT1", target="t1", detail="d1"
        )
        await test_db.flush()
        log2 = await append_audit_log(
            test_db, operator="u2", action="ACT2", target="t2", detail="d2"
        )
        await test_db.flush()
        log3 = await append_audit_log(
            test_db, operator="u3", action="ACT3", target="t3", detail="d3"
        )
        await test_db.commit()

        assert log1.seq == 1
        assert log2.seq == 2
        assert log3.seq == 3
        assert log1.prev_hash == "0" * 64
        assert log2.prev_hash == log1.current_hash
        assert log3.prev_hash == log2.current_hash

    async def test_append_with_entry_object(self, test_db: AsyncSession):
        """支持传入 AuditLogCreate 对象的调用方式"""
        entry = AuditLogCreate(
            operator="entry-tester",
            action="ENTRY_TEST",
            target="dev-entry",
            evidence_refs=["E1", "E2"],
            ai_model="test-model",
            ai_output_hash="sha256:abc",
        )
        log = await append_audit_log(test_db, entry)
        await test_db.commit()

        assert log.operator == "entry-tester"
        assert log.action == "ENTRY_TEST"
        assert log.ai_model == "test-model"
        assert log.ai_output_hash == "sha256:abc"

    async def test_kwargs_overrides_entry_session_id(self, test_db: AsyncSession):
        """kwargs 模式下 session_id 正确写入"""
        log = await append_audit_log(
            test_db,
            operator="u",
            action="A",
            session_id="sess-123",
        )
        await test_db.commit()
        assert log.session_id == "sess-123"


class TestVerifyHashChain:
    """verify_hash_chain 篡改检测测试"""

    async def test_verify_intact_chain(self, test_db: AsyncSession):
        """完整链 verify 返回 True"""
        for i in range(5):
            await append_audit_log(
                test_db,
                operator=f"u{i}",
                action=f"ACT{i}",
                target=f"t{i}",
                detail=f"d{i}",
            )
            await test_db.flush()
        await test_db.commit()

        valid = await verify_hash_chain(test_db)
        assert valid is True

    async def test_verify_detects_tampered_detail(self, test_db: AsyncSession):
        """篡改 detail 字段后 verify 返回 False"""
        await append_audit_log(
            test_db, operator="u1", action="A1", detail="原始详情"
        )
        await test_db.flush()
        await append_audit_log(
            test_db, operator="u2", action="A2", detail="第二条"
        )
        await test_db.commit()

        # 直接修改 DB（绕过应用层）—— 模拟 DBA 篡改
        from sqlalchemy import update

        from shared.models import AuditLog

        await test_db.execute(
            update(AuditLog)
            .where(AuditLog.seq == 1)
            .values(detail="篡改后的详情")
        )
        await test_db.commit()

        # SQLite 无 RULE/触发器防护，应用层 verify 应检测到篡改
        valid = await verify_hash_chain(test_db)
        assert valid is False, "篡改 detail 后应检测到"

    async def test_verify_empty_chain(self, test_db: AsyncSession):
        """空链 verify 返回 True"""
        valid = await verify_hash_chain(test_db)
        assert valid is True

    async def test_verify_detects_broken_link(self, test_db: AsyncSession):
        """prev_hash 链断裂应被检测到"""
        await append_audit_log(
            test_db, operator="u1", action="A1", detail="d1"
        )
        await test_db.flush()
        await append_audit_log(
            test_db, operator="u2", action="A2", detail="d2"
        )
        await test_db.commit()

        # 篡改 log2 的 prev_hash 使链断裂
        from sqlalchemy import update

        from shared.models import AuditLog

        await test_db.execute(
            update(AuditLog)
            .where(AuditLog.seq == 2)
            .values(prev_hash="tampered_hash_value_xxx")
        )
        await test_db.commit()

        valid = await verify_hash_chain(test_db)
        assert valid is False

    async def test_concurrent_append_same_prev_hash(self, test_db: AsyncSession):
        """连续追加：seq 单调递增且 hash 链完整

        注：单个 AsyncSession 不能被多个协程并发 flush（SQLAlchemy 会话非并发安全，
        会抛 "Session is already flushing"）；生产环境的并发串行化由 PostgreSQL
        advisory lock 保证，此处仅验证可观察契约——seq 无重复、链完整。
        """
        results = [
            await append_audit_log(
                test_db, operator=f"u{i}", action="CONC", target="conc", detail=f"detail-{i}"
            )
            for i in range(5)
        ]
        await test_db.commit()

        # 检查 seq 是否单调递增（1, 2, 3, 4, 5），无重复无跳跃
        seqs = sorted([r.seq for r in results])
        assert seqs == [1, 2, 3, 4, 5], f"seq 应单调递增，得到 {seqs}"

        # 检查 hash 链是否完整
        valid = await verify_hash_chain(test_db)
        assert valid is True
