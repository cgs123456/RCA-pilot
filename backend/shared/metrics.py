"""Prometheus metrics —— 4 黄金信号（延迟/流量/错误/饱和度）

大厂标准：
- RED 方法（Rate, Errors, Duration）针对每个端点
- 资源饱和度（DB 连接池、内存）
- 业务指标（案例数、Agent 运行数、审计日志数）
"""
from __future__ import annotations

import time
from typing import Optional

from fastapi import FastAPI, Request
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

# ============================================================
# 全局指标定义（所有服务共享）
# ============================================================
REGISTRY = CollectorRegistry()

# 1. 流量（Rate）—— 每个端点+方法的请求总数
REQUEST_COUNT = Counter(
    "http_requests_total",
    "HTTP 请求总数",
    labelnames=("service", "method", "endpoint", "status"),
    registry=REGISTRY,
)

# 2. 延迟（Duration）—— P50/P90/P99
REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP 请求延迟（秒）",
    labelnames=("service", "method", "endpoint"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=REGISTRY,
)

# 3. 错误（Errors）—— 5xx 错误数
REQUEST_ERRORS = Counter(
    "http_errors_total",
    "HTTP 5xx 错误总数",
    labelnames=("service", "method", "endpoint", "status"),
    registry=REGISTRY,
)

# 4. 饱和度（Saturation）—— DB 连接池使用率
DB_POOL_SIZE = Gauge(
    "db_pool_size",
    "DB 连接池大小",
    labelnames=("service",),
    registry=REGISTRY,
)
DB_POOL_CHECKED_OUT = Gauge(
    "db_pool_checked_out",
    "DB 连接池已检出连接数",
    labelnames=("service",),
    registry=REGISTRY,
)

# 5. 业务指标
CASES_TOTAL = Gauge(
    "rca_cases_total",
    "偏差案例总数",
    labelnames=("status", "severity"),
    registry=REGISTRY,
)
AGENT_RUNS_TOTAL = Counter(
    "rca_agent_runs_total",
    "Agent 流水线运行总数",
    labelnames=("case_id", "status"),
    registry=REGISTRY,
)
AUDIT_LOGS_TOTAL = Gauge(
    "rca_audit_logs_total",
    "审计日志总条数",
    registry=REGISTRY,
)

# 6. 限流计数
RATE_LIMITED_TOTAL = Counter(
    "rate_limited_total",
    "被限流的请求总数",
    labelnames=("service", "endpoint"),
    registry=REGISTRY,
)


class MetricsMiddleware(BaseHTTPMiddleware):
    """Prometheus 指标收集中间件

    每个请求自动记录：
    - 请求总数（按 method/endpoint/status 分维）
    - 请求延迟（直方图）
    - 5xx 错误数
    """

    def __init__(self, app, service_name: str):
        super().__init__(app)
        self.service_name = service_name

    async def dispatch(self, request: Request, call_next):
        # 跳过 /metrics /health /ready 不计入业务指标
        path = request.url.path
        if path in ("/metrics", "/health", "/ready", "/docs", "/redoc", "/openapi.json"):
            return await call_next(request)

        start_time = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception:
            status_code = 500
            raise
        finally:
            duration = time.perf_counter() - start_time
            method = request.method
            # 用路由 path 而非完整 URL（避免高基数）
            endpoint = self._get_route_path(request) or path

            REQUEST_COUNT.labels(
                service=self.service_name,
                method=method,
                endpoint=endpoint,
                status=str(status_code),
            ).inc()
            REQUEST_LATENCY.labels(
                service=self.service_name,
                method=method,
                endpoint=endpoint,
            ).observe(duration)
            if status_code >= 500:
                REQUEST_ERRORS.labels(
                    service=self.service_name,
                    method=method,
                    endpoint=endpoint,
                    status=str(status_code),
                ).inc()

    @staticmethod
    def _get_route_path(request: Request) -> Optional[str]:
        """从 request.scope 提取路由模板（如 /cases/{case_id}）"""
        route = request.scope.get("route")
        if route and hasattr(route, "path"):
            return route.path
        return None


def register_metrics_endpoint(app: FastAPI) -> None:
    """注册 /metrics 端点 —— Prometheus 抓取"""

    @app.get("/metrics", tags=["meta"], include_in_schema=False)
    async def metrics():
        """Prometheus metrics 端点"""
        # 更新 DB 连接池指标
        try:
            from shared.database import engine
            pool = engine.pool
            DB_POOL_SIZE.labels(service=app.title).set(pool.size())
            DB_POOL_CHECKED_OUT.labels(service=app.title).set(pool.checkedout())
        except Exception:
            pass

        return Response(
            content=generate_latest(REGISTRY),
            media_type=CONTENT_TYPE_LATEST,
        )
