"""Agent 流水线 DB 轮询 worker（C7 残留修复）

BackgroundTasks 的流水线随 API 进程死亡而丢失（C7）——本 worker 让 run 持久化在
DB 里（status="pending"），由 agent-service 进程内的轮询循环认领执行：

- 认领是原子条件更新（UPDATE ... WHERE status='pending'），BackgroundTasks 快路径
  与 worker 慢路径并发认领同一 run 时只有一方成功，另一方静默退出 —— 天然幂等，
  多 worker/多实例下每个 run 也恰被执行一次（条件更新的行级原子性）。
- 进程重启后启动恢复把中断的 running 重新排队为 pending，worker 自动续跑 ——
  流水线不再随进程死亡而"永远失败"。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update

from shared.config import settings

logger = logging.getLogger("agent-worker")

# 并发执行的流水线上限（每个 run 由 LLM Semaphore(8) 进一步限流）
_pipeline_slots: asyncio.Semaphore | None = None
_active_tasks: set[asyncio.Task[Any]] = set()


def _slots() -> asyncio.Semaphore:
    global _pipeline_slots
    if _pipeline_slots is None:
        _pipeline_slots = asyncio.Semaphore(max(1, int(settings.agent_pipeline_concurrency)))
    return _pipeline_slots


async def claim_pending_run(run_id: str) -> bool:
    """原子认领：仅当 run 仍为 pending 时置为 running。返回是否认领成功。"""
    from shared.database import db_session
    from shared.models import AgentRun

    async with db_session() as db:
        result = await db.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id, AgentRun.status == "pending")
            .values(status="running", started_at=datetime.now(UTC))
        )
        await db.commit()
        return bool(result.rowcount)


async def claim_next_pending_run() -> tuple[str, str, str] | None:
    """认领最早的 pending run。返回 (case_id, run_id, triggered_by)。"""
    from shared.database import db_session
    from shared.models import AgentRun

    async with db_session() as db:
        row = (
            await db.execute(
                select(AgentRun.id, AgentRun.case_id, AgentRun.triggered_by)
                .where(AgentRun.status == "pending")
                .order_by(AgentRun.created_at)
                .limit(1)
                .execution_options(synchronize_session=False)
            )
        ).first()
        if not row:
            return None
        claimed = await db.execute(
            update(AgentRun)
            .where(AgentRun.id == row[0], AgentRun.status == "pending")
            .values(status="running", started_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        if not claimed.rowcount:
            return None
        return (row[1], row[0], row[2] or "system")


async def _run_claimed(case_id: str, run_id: str, triggered_by: str) -> None:
    from agent_service.routes import _execute_pipeline

    async with _slots():
        await _execute_pipeline(case_id, run_id, triggered_by, claimed=True)


async def worker_loop(poll_seconds: float | None = None, *, once: bool = False) -> None:
    """轮询认领 pending run 并并发执行；once=True 只扫一轮（测试用）。"""
    interval = (
        poll_seconds
        if poll_seconds is not None
        else max(0.1, float(settings.agent_worker_poll_seconds))
    )
    while True:
        try:
            claimed = await claim_next_pending_run()
        except Exception:
            logger.warning("pending run 认领查询失败（下个轮询周期重试）", exc_info=True)
            claimed = None
        if claimed:
            case_id, run_id, triggered_by = claimed
            task = asyncio.create_task(_run_claimed(case_id, run_id, triggered_by))
            _active_tasks.add(task)
            task.add_done_callback(_active_tasks.discard)
        if once:
            # 测试模式：扫一轮后等已派发的流水线跑完，避免测试在任务执行中途断言
            if _active_tasks:
                await asyncio.gather(*_active_tasks, return_exceptions=True)
            return
        await asyncio.sleep(interval if not claimed else 0.0)
