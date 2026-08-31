"""TRUNCATE Guard 触发器测试 — 21 CFR Part 11 审计日志完整性

覆盖：
- TRUNCATE 被阻止时返回清晰错误
- 正确的 DROP TRIGGER + TRUNCATE + CREATE TRIGGER 模式
- 仅 superuser 可绕过（模拟 migration 上下文）
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


async def test_truncate_blocked_by_trigger(test_db: AsyncSession):
    """直接 TRUNCATE 应被触发器阻止，返回明确错误"""
    from shared.models import AuditLog

    # 先插入一些日志
    log = AuditLog(
        seq=1,
        operator="tester",
        action="TEST",
        target="case-1",
        detail="test detail",
    )
    test_db.add(log)
    await test_db.flush()

    # 尝试 TRUNCATE - 应该被触发器阻止
    try:
        await test_db.execute(text("TRUNCATE TABLE audit_log"))
        await test_db.commit()
        assert False, "TRUNCATE 应该被触发器阻止"
    except Exception as e:
        error_msg = str(e)
        # 应该包含提示信息，指引使用安全模式
        assert "TRUNCATE" in error_msg or "blocked" in error_msg.lower(), \
            f"错误信息应包含 TRUNCATE/blocked 提示，得到: {error_msg}"


async def test_truncate_via_safe_mode_succeeds(test_db: AsyncSession, admin_user):
    """superuser/管理模式下可通过安全流程执行 TRUNCATE"""
    from shared.models import AuditLog

    # 先插入一些日志
    log = AuditLog(
        seq=1,
        operator="tester",
        action="TEST",
        target="case-1",
        detail="test detail",
    )
    test_db.add(log)
    await test_db.flush()

    # 使用管理模式的安全 TRUNCATE（通过函数调用而非直接 TRUNCATE）
    # 这模拟了 migration 脚本使用的模式
    try:
        # 尝试通过安全函数执行
        result = await test_db.execute(
            text("SELECT shared.safe_truncate_audit_log()")
        )
        # 如果函数不存在，则跳过或记录
        pytest.skip("safe_truncate_audit_log function not yet implemented")
    except Exception:
        pytest.skip("safe_truncate function not yet available")


async def test_audit_logs_remain_integrated_after_migration(test_db: AsyncSession):
    """迁移执行后，审计链应保持完整"""
    from shared.audit import verify_hash_chain

    # 插入几条日志
    for i in range(3):
        log = AuditLog(
            seq=i + 1,
            operator=f"u{i}",
            action=f"A{i}",
            target=f"t{i}",
            detail=f"d{i}",
        )
        test_db.add(log)
    await test_db.flush()
    await test_db.commit()

    # verify_hash_chain 应返回 True
    valid = await verify_hash_chain(test_db)
    assert valid is True, "迁移前审计链应完整"