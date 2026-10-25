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
        reload=(reload_flag := os.getenv("ENV") == "dev"),
        # H28 水平扩容：经 UVICORN_WORKERS 开多 worker（与 dev 的 reload 互斥）
        workers=1 if reload_flag else max(1, int(os.getenv("UVICORN_WORKERS", "1"))),
        log_config=None,
    )
