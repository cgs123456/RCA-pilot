"""FastAPI 应用工厂 — 共享启动逻辑（大厂标准版）

集成：
- TraceId 中间件（请求贯穿）
- 全局异常处理器（统一错误格式 + Sentry 上报）
- CORS
- Prometheus metrics（4 黄金信号）
- slowapi 限流
- /health + /ready + /metrics 端点
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator, Callable

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .error_handler import TraceIdMiddleware, register_exception_handlers
from .logging import setup_logging


def create_app(
    service_name: str,
    *,
    version: str = "1.0.0",
    lifespan: Callable | None = None,
    enable_metrics: bool = True,
    enable_rate_limit: bool = True,
) -> FastAPI:
    """FastAPI 应用工厂 — 所有微服务共用

    Args:
        service_name: 服务名（如 "auth_service"）
        version: 服务版本
        lifespan: 自定义生命周期（覆盖默认 logging 初始化）
        enable_metrics: 是否启用 Prometheus metrics
        enable_rate_limit: 是否启用 slowapi 限流
    """

    @asynccontextmanager
    async def default_lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        setup_logging(service_name)
        # Sentry 初始化（如果配置了 DSN）
        if settings.sentry_dsn:
            import sentry_sdk
            from sentry_sdk.integrations.fastapi import FastApiIntegration
            from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration

            sentry_sdk.init(
                dsn=settings.sentry_dsn,
                environment=settings.env,
                traces_sample_rate=0.1,
                send_default_pii=False,
                integrations=[
                    FastApiIntegration(),
                    SqlalchemyIntegration(),
                ],
            )
        yield

    app = FastAPI(
        title=f"RCA Pilot — {service_name}",
        version=version,
        description="受 GxP 监管的 Agentic AI 偏差调查平台",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url=f"/{service_name}/openapi.json",
        lifespan=lifespan or default_lifespan,
    )

    # ============================================================
    # 中间件（顺序：后添加的先执行）
    # ============================================================
    # CORS —— 最外层
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Trace-Id"],
    )

    # TraceId 中间件 —— 每个请求分配/透传 traceId
    app.add_middleware(TraceIdMiddleware)

    # Prometheus metrics 中间件
    if enable_metrics:
        from .metrics import MetricsMiddleware, register_metrics_endpoint

        app.add_middleware(MetricsMiddleware, service_name=service_name)
        register_metrics_endpoint(app)

    # slowapi 限流
    if enable_rate_limit:
        from .rate_limit import init_rate_limiter

        init_rate_limiter(app)

    # ============================================================
    # 全局异常处理器（统一错误格式）
    # ============================================================
    register_exception_handlers(app)

    # ============================================================
    # 健康检查端点
    # ============================================================
    @app.get("/health", tags=["meta"])
    async def health():
        """存活检查 —— 只要进程在跑就返回 ok"""
        return {"status": "ok", "service": service_name, "env": settings.env}

    @app.get("/ready", tags=["meta"])
    async def readiness():
        """就绪检查 —— 检查 DB/Redis 连通性"""
        from sqlalchemy import text
        from .database import AsyncSessionLocal

        checks = {"service": service_name}
        try:
            async with AsyncSessionLocal() as session:
                await session.execute(text("SELECT 1"))
            checks["db"] = "ok"
        except Exception as e:
            checks["db"] = f"fail: {type(e).__name__}"

        all_ok = all(v == "ok" for k, v in checks.items() if k != "service")
        return {
            "status": "ready" if all_ok else "not_ready",
            **checks,
        }

    return app
