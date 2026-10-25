"""C7 流水线 DB 轮询 worker 测试 — 认领幂等 + 启动恢复重排队 + 端到端执行"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import shared.database as sdb
from agent_service import pipeline as agent_pipeline
from agent_service import routes as agent_routes
from agent_service.worker import (
    claim_next_pending_run,
    claim_pending_run,
    worker_loop,
)
from shared.models import AgentRun, AuditLog, Deviation

pytestmark = pytest.mark.asyncio

FRESH = datetime.now(UTC)
STALE = FRESH - timedelta(minutes=30)


@pytest.fixture
async def patch_db(test_engine, monkeypatch):
    """把 worker/recovery 用的全局 db_session 指到测试引擎

    worker 与 recover_zombie_runs 各自持有 db_session 绑定：
    - agent_service.worker 惰性 import shared.database.db_session
    - agent_service.routes 顶层 import（自有绑定）
    """
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def _session() -> Any:
        async with factory() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    monkeypatch.setattr(sdb, "db_session", _session)
    # P3-① 拆分后 pipeline/recover 的会话绑定在 agent_service.pipeline（routes 不再持有会话）
    monkeypatch.setattr(agent_pipeline, "db_session", _session)
    return factory


async def _seed_case(factory, case_id: str = "DEV-WORKER-1") -> None:
    async with factory() as s:
        s.add(Deviation(
            id=case_id, title="worker 测试", severity="minor", status="investigating",
            discovered_at=FRESH, investigator_id="U-LC", progress=0,
            hitl_gates={f"gate{i}": "pending" for i in range(1, 6)},
        ))
        await s.commit()


async def _add_run(factory, run_id: str, status: str, started_at: datetime | None = None,
                   created_at: datetime | None = None, case_id: str = "DEV-WORKER-1") -> None:
    async with factory() as s:
        s.add(AgentRun(
            id=run_id, case_id=case_id, status=status, triggered_by="U-LC",
            agent_states={}, started_at=started_at, created_at=created_at or FRESH,
        ))
        await s.commit()


async def _get_run(factory, run_id: str) -> AgentRun:
    async with factory() as s:
        run = (await s.execute(select(AgentRun).where(AgentRun.id == run_id))).scalar_one()
        assert isinstance(run, AgentRun)
        s.expunge(run)
        return run


class TestClaim:
    async def test_claim_pending_once(self, patch_db):
        await _seed_case(patch_db)
        await _add_run(patch_db, "RUN-1", "pending")
        assert await claim_pending_run("RUN-1") is True
        run = await _get_run(patch_db, "RUN-1")
        assert run.status == "running"
        assert run.started_at is not None

    async def test_claim_twice_second_fails(self, patch_db):
        """认领幂等：BackgroundTasks 快路径与 worker 并发认领只有一方成功"""
        await _seed_case(patch_db)
        await _add_run(patch_db, "RUN-1", "pending")
        assert await claim_pending_run("RUN-1") is True
        assert await claim_pending_run("RUN-1") is False

    async def test_claim_skips_non_pending(self, patch_db):
        await _seed_case(patch_db)
        for st in ("running", "paused", "failed", "completed", "cancelled"):
            await _add_run(patch_db, f"RUN-{st}", st)
            assert await claim_pending_run(f"RUN-{st}") is False

    async def test_claim_next_picks_oldest(self, patch_db):
        """uq_agentruns_one_active_per_case：不同 case 各一条 pending，按创建顺序认领"""
        await _seed_case(patch_db)
        await _seed_case(patch_db, "DEV-WORKER-2")
        old = FRESH - timedelta(minutes=5)
        await _add_run(patch_db, "RUN-OLD", "pending", created_at=old)
        await _add_run(patch_db, "RUN-NEW", "pending", created_at=FRESH, case_id="DEV-WORKER-2")
        first = await claim_next_pending_run()
        assert first == ("DEV-WORKER-1", "RUN-OLD", "U-LC")
        second = await claim_next_pending_run()
        assert second == ("DEV-WORKER-2", "RUN-NEW", "U-LC")
        assert await claim_next_pending_run() is None


class TestRecovery:
    async def test_stale_running_requeued(self, patch_db):
        """C7: 中断的 running 重新排队 pending（而非 failed），worker 可自动续跑"""
        await _seed_case(patch_db)
        await _add_run(patch_db, "RUN-STALE", "running", started_at=STALE)
        requeued = await agent_routes.recover_zombie_runs()
        assert requeued == 1
        run = await _get_run(patch_db, "RUN-STALE")
        assert run.status == "pending"
        assert run.started_at is None
        audit = (await _audit(patch_db, "AGENT_PIPELINE_REQUEUED"))
        assert audit is not None

    async def test_fresh_running_untouched(self, patch_db):
        """多实例部署：其他实例正在执行的 run（未超阈值）不动"""
        await _seed_case(patch_db)
        await _add_run(patch_db, "RUN-FRESH", "running", started_at=FRESH)
        assert await agent_routes.recover_zombie_runs() == 0
        assert (await _get_run(patch_db, "RUN-FRESH")).status == "running"

    async def test_pending_not_touched(self, patch_db):
        """pending 是 worker 的正常队列，恢复逻辑不越权改写"""
        await _seed_case(patch_db)
        await _add_run(patch_db, "RUN-QUEUED", "pending", started_at=STALE)
        assert await agent_routes.recover_zombie_runs() == 0
        assert (await _get_run(patch_db, "RUN-QUEUED")).status == "pending"


async def _audit(factory, action: str) -> AuditLog | None:
    async with factory() as s:
        row: AuditLog | None = (
            await s.execute(select(AuditLog).where(AuditLog.action == action))
        ).scalars().first()
        if row:
            s.expunge(row)
        return row


class TestEndToEnd:
    async def test_worker_loop_executes_pending_run(self, patch_db):
        """端到端：worker_loop(once=True) 认领并以 rule 路径跑完整流水线"""
        await _seed_case(patch_db)
        await _add_run(patch_db, "RUN-E2E", "pending")
        await worker_loop(poll_seconds=0.01, once=True)
        run = await _get_run(patch_db, "RUN-E2E")
        assert run.status == "completed"
        states = run.agent_states or {}
        for agent in ("meta_controller", "evidence_hunter", "reasoner", "skeptic", "drafter", "guardian"):
            assert agent in states, f"缺少 {agent} 阶段输出"

    async def test_execute_pipeline_skips_unclaimed(self, patch_db):
        """未认领（running 已被他人持有）时快路径静默退出，不产生输出"""
        await _seed_case(patch_db)
        await _add_run(patch_db, "RUN-BUSY", "running", started_at=FRESH)
        await agent_routes._execute_pipeline("DEV-WORKER-1", "RUN-BUSY", "U-LC")
        run = await _get_run(patch_db, "RUN-BUSY")
        assert run.status == "running"
        assert not (run.agent_states or {})
