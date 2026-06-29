"""Agent Service 启动入口"""
from __future__ import annotations

import os

import uvicorn

from shared.service_base import create_app
from agent_service.routes import router

app = create_app("agent-service")
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
