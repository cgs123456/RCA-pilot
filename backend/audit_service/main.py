"""Audit Service 启动入口"""
from __future__ import annotations

import os

import uvicorn

from shared.service_base import create_app
from audit_service.routes import router

app = create_app("audit-service")
app.include_router(router, prefix="/api/v1")


if __name__ == "__main__":
    port = int(os.getenv("AUDIT_SERVICE_PORT", "8005"))
    uvicorn.run(
        "audit_service.main:app",
        host="0.0.0.0",
        port=port,
        reload=os.getenv("ENV") == "dev",
        log_config=None,
    )
