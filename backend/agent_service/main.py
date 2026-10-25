"""Agent Service 启动入口"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager

import uvicorn

from agent_service.routes import recover_zombie_runs, router
from agent_service.worker import worker_loop
from shared.config import settings
from shared.service_base import create_app, spawn_logged_task

logger = logging.getLogger("agent-service")


@asynccontextmanager
async def lifespan(app):
    """启动恢复（C7：中断 run 重新排队）+ DB 轮询 worker（流水线持久化执行）"""
    requeued = await recover_zombie_runs()
    if requeued:
        logger.info("startup recovery: %d interrupted run(s) requeued", requeued)
    task = None
    if settings.agent_worker_enabled:
        task = spawn_logged_task(worker_loop(), logger, "agent-worker")
    yield
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = create_app("agent-service", lifespan=lifespan)
app.include_router(router, prefix="/api/v1")


if __name__ == "__main__":
    port = int(os.getenv("AGENT_SERVICE_PORT", "8004"))
    uvicorn.run(
        "agent_service.main:app",
        host="0.0.0.0",
        port=port,
        reload=(reload_flag := os.getenv("ENV") == "dev"),
        # H28 水平扩容：经 UVICORN_WORKERS 开多 worker（与 dev 的 reload 互斥）
        workers=1 if reload_flag else max(1, int(os.getenv("UVICORN_WORKERS", "1"))),
        log_config=None,
    )
