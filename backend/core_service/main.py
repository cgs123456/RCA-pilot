"""Core Service — P3-① 架构收敛单元

合并 auth / case / evidence / audit 四个业务服务到同一 FastAPI 应用
（单租户部署不需要 9 个独立进程；agent 保留独立单元以隔离 LLM 长任务，
connectors 收敛为 connector-gateway）。

- 限流：slowapi 限流池在合并后为全单元共享（按 IP 计数，语义更严格但口径一致）
- 指标：http_requests_total{service="core-service"} 覆盖四个领域（按 endpoint 区分）
- 审计锚定：复用 audit_service.main.anchor_periodically 生命周期任务
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager

import uvicorn

from audit_service.main import anchor_periodically
from audit_service.routes import router as audit_router
from auth_service.routes import router as auth_router
from case_service.capa_routes import router as capa_router
from case_service.notification_routes import router as notification_router
from case_service.report_routes import router as report_router
from case_service.routes import router as case_router
from evidence_service.routes import router as evidence_router
from shared.service_base import create_app, spawn_logged_task

logger = logging.getLogger("core-service")


@asynccontextmanager
async def lifespan(app):
    task = spawn_logged_task(anchor_periodically(), logger, "audit-anchor")
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


app = create_app("core-service", lifespan=lifespan)
app.include_router(auth_router, prefix="/api/v1")
# case 领域 = 主路由 + CAPA/报告/通知三个子路由（缺一即部署态 404，有契约测试锁定）
app.include_router(case_router, prefix="/api/v1")
app.include_router(capa_router, prefix="/api/v1")
app.include_router(report_router, prefix="/api/v1")
app.include_router(notification_router, prefix="/api/v1")
app.include_router(evidence_router, prefix="/api/v1")
app.include_router(audit_router, prefix="/api/v1")


if __name__ == "__main__":
    port = int(os.getenv("CORE_SERVICE_PORT", "8000"))
    uvicorn.run(
        "core_service.main:app",
        host="0.0.0.0",
        port=port,
        reload=(reload_flag := os.getenv("ENV") == "dev"),
        # H28 水平扩容：经 UVICORN_WORKERS 开多 worker（与 dev 的 reload 互斥）
        workers=1 if reload_flag else max(1, int(os.getenv("UVICORN_WORKERS", "1"))),
        log_config=None,
    )
