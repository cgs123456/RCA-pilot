"""Connector Gateway — P3-① 架构收敛单元

把 LMS/BMS/EMS/MES 四个 mock connector 合并到一个 FastAPI 应用
（单容器暴露 :8010，路由前缀不变：/api/v1/{lms,bms,ems,mes}/...）。
evidence_service 经 settings.{lms,bms,ems,mes}_connector_url 访问本网关。
"""
from __future__ import annotations

import os

import uvicorn

from connectors.bms.routes import router as bms_router
from connectors.ems.routes import router as ems_router
from connectors.lms.routes import router as lms_router
from connectors.mes.routes import router as mes_router
from shared.service_base import create_app

app = create_app("connector-gateway", version="1.0.0")
app.include_router(lms_router, prefix="/api/v1")
app.include_router(bms_router, prefix="/api/v1")
app.include_router(ems_router, prefix="/api/v1")
app.include_router(mes_router, prefix="/api/v1")


if __name__ == "__main__":
    port = int(os.getenv("CONNECTOR_GATEWAY_PORT", "8010"))
    uvicorn.run(
        "connectors.gateway:app",
        host="0.0.0.0",
        port=port,
        reload=(reload_flag := os.getenv("ENV") == "dev"),
        workers=1 if reload_flag else max(1, int(os.getenv("UVICORN_WORKERS", "1"))),
        log_config=None,
    )
