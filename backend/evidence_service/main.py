"""Evidence Service 启动入口"""
from __future__ import annotations

import os

import uvicorn

from evidence_service.routes import router
from shared.service_base import create_app

app = create_app("evidence-service")
app.include_router(router, prefix="/api/v1")


if __name__ == "__main__":
    port = int(os.getenv("EVIDENCE_SERVICE_PORT", "8003"))
    uvicorn.run(
        "evidence_service.main:app",
        host="0.0.0.0",
        port=port,
        reload=os.getenv("ENV") == "dev",
        log_config=None,
    )
