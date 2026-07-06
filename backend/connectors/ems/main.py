"""EMS Mock Connector 启动入口"""
from __future__ import annotations

import os

import uvicorn

from shared.service_base import create_app
from connectors.ems.routes import router

app = create_app("ems-connector", version="1.0.0")
app.include_router(router, prefix="/api/v1")


if __name__ == "__main__":
    port = int(os.getenv("EMS_CONNECTOR_PORT", "8013"))
    uvicorn.run(
        "connectors.ems.main:app",
        host="0.0.0.0",
        port=port,
        reload=os.getenv("ENV") == "dev",
        log_config=None,
    )
