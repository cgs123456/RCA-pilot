"""Agent Service 启动入口"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

import uvicorn

from agent_service.routes import recover_zombie_runs, router
from shared.service_base import create_app


@asynccontextmanager
async def lifespan(app):
    """启动时恢复中断遗留的僵尸 run（GxP: 失败尝试也必须留痕）"""
    recovered = await recover_zombie_runs()
    if recovered:
        print(f"[AGENT] startup recovery: {recovered} interrupted run(s) marked failed")
    yield


app = create_app("agent-service", lifespan=lifespan)
app.include_router(router, prefix="/api/v1")


if __name__ == "__main__":
    port = int(os.getenv("AGENT_SERVICE_PORT", "8004"))
    uvicorn.run(
        "agent_service.main:app",
        host="0.0.0.0",
        port=port,
        reload=os.getenv("ENV") == "dev",
        log_config=None,
    )
