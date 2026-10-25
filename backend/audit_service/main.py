"""Audit Service 启动入口"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager

import uvicorn

from audit_service.routes import router
from shared.config import settings
from shared.database import db_session
from shared.service_base import create_app, spawn_logged_task

logger = logging.getLogger("audit-service")


async def anchor_periodically() -> None:
    """B9 周期锚定：把审计链头定期追加到外部只读存储（audit_anchor_path）。

    单次失败只告警不退出 —— 锚定是尽力而为的外部证据，DB 不可用时重试即可。
    core-service 合并部署时复用本协程（P3-① 架构收敛）。
    """
    interval = max(0, int(settings.audit_anchor_interval_seconds))
    if interval <= 0:
        return
    while True:
        await asyncio.sleep(interval)
        try:
            from shared.audit_anchor import anchor_chain_head

            async with db_session() as db:
                record = await anchor_chain_head(db)
            if record:
                logger.info("审计链头已锚定 seq=%s", record["seq"])
        except Exception:
            logger.warning("审计链头周期锚定失败（下个周期重试）", exc_info=True)


@asynccontextmanager
async def lifespan(app):
    task = spawn_logged_task(anchor_periodically(), logger, "audit-anchor")
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


app = create_app("audit-service", lifespan=lifespan)
app.include_router(router, prefix="/api/v1")


if __name__ == "__main__":
    port = int(os.getenv("AUDIT_SERVICE_PORT", "8005"))
    uvicorn.run(
        "audit_service.main:app",
        host="0.0.0.0",
        port=port,
        reload=(reload_flag := os.getenv("ENV") == "dev"),
        # H28 水平扩容：经 UVICORN_WORKERS 开多 worker（与 dev 的 reload 互斥）
        workers=1 if reload_flag else max(1, int(os.getenv("UVICORN_WORKERS", "1"))),
        log_config=None,
    )
