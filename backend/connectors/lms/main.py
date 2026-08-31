"""LMS Mock Connector 启动入口"""
from __future__ import annotations

import os

import uvicorn

from connectors.lms.routes import router
from shared.service_base import create_app

app = create_app("lms-connector", version="1.0.0")
app.include_router(router, prefix="/api/v1")


if __name__ == "__main__":
    port = int(os.getenv("LMS_CONNECTOR_PORT", "8011"))
    uvicorn.run(
        "connectors.lms.main:app",
        host="0.0.0.0",
        port=port,
        reload=os.getenv("ENV") == "dev",
        log_config=None,
    )
