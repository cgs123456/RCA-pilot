"""Prometheus metrics —— 4 黄金信号（延迟/流量/错误/饱和度）

大厂标准：
- RED 方法（Rate, Errors, Duration）针对每个端点
- 资源饱和度（DB 连接池、内存）
- 业务指标（案例数、Agent 运行数、审计日志数）
"""
from __future__ import annotations

import os
import time

from fastapi import FastAPI, Request
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from sqlalchemy.pool import QueuePool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

# uvicorn 多 worker（H28）时单进程 registry 只能看到抓取命中的那一个 worker，
# 必须启用 prometheus_client multiprocess 模式聚合全部进程（compose 已设该变量）。
_MULTIPROC_DIR = os.getenv("PROMETHEUS_MULTIPROC_DIR")

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
# multiprocess 模式下 gauge 需显式聚合语义：size 各 worker 相同取 max，
# checked_out 是各 worker 各自持连数取 livesum，否则告警表达式会出现多对多匹配错误。
DB_POOL_SIZE = Gauge(
    "db_pool_size",
    "DB 连接池大小",
    labelnames=("service",),
    registry=REGISTRY,
    multiprocess_mode="max",
)
DB_POOL_CHECKED_OUT = Gauge(
    "db_pool_checked_out",
    "DB 连接池已检出连接数",
    labelnames=("service",),
    registry=REGISTRY,
    multiprocess_mode="livesum",
)

# 5. 业务指标
CASES_TOTAL = Gauge(
    "rca_cases_total",
    "偏差案例总数",
    labelnames=("status", "severity"),
    registry=REGISTRY,
    multiprocess_mode="max",
)
# 注意：不用 case_id 作 label —— 无界高基数会拖垮 Prometheus（按 status 聚合即可）
AGENT_RUNS_TOTAL = Counter(
    "rca_agent_runs_total",
    "Agent 流水线运行总数",
    labelnames=("status",),
    registry=REGISTRY,
)
AUDIT_LOGS_TOTAL = Gauge(
    "rca_audit_logs_total",
    "审计日志总条数",
    registry=REGISTRY,
    multiprocess_mode="max",
)

# 6. 限流计数
RATE_LIMITED_TOTAL = Counter(
    "rate_limited_total",
    "被限流的请求总数",
    labelnames=("service", "endpoint"),
    registry=REGISTRY,
)

# 7. LLM / Agent 流水线业务指标（成本与性能可观测性）
LLM_TOKENS_TOTAL = Counter(
    "rca_llm_tokens_total",
    "LLM token 用量累计",
    labelnames=("agent", "model", "kind"),  # kind: prompt | completion
    registry=REGISTRY,
)
LLM_CALLS_TOTAL = Counter(
    "rca_llm_calls_total",
    "LLM 调用次数",
    labelnames=("agent", "model", "outcome"),  # outcome: success | error
    registry=REGISTRY,
)

# LLM 成本估算价表（每百万 token，人民币）：(prompt, completion)。
# 按各厂商 2026 年初公开牌价粗略记录，前缀匹配取第一个命中；仅用于相对监控与
# 容量规划，不作为财务口径。未收录模型成本按 0 计（token 计数仍准确）。
MODEL_COST_PER_MTOKEN: tuple[tuple[str, tuple[float, float]], ...] = (
    ("glm-4-plus", (50.0, 50.0)),
    ("glm-4", (0.1, 0.1)),  # glm-4-air/flash 等低价位档
    ("qwen-plus", (0.8, 2.0)),
    ("qwen-max", (2.4, 9.6)),
    ("gpt-4o", (2.5, 10.0)),
    ("claude-sonnet", (3.0, 15.0)),
    ("embedding", (0.5, 0.0)),
)
LLM_COST_ESTIMATED_TOTAL = Counter(
    "rca_llm_cost_estimated_cny_total",
    "LLM 成本估算累计（人民币元，按单价表估算而非账单）",
    labelnames=("agent", "model"),
    registry=REGISTRY,
)


def record_llm_cost(agent: str, model: str, prompt_tokens: int, completion_tokens: int) -> None:
    """按单价表估算并累计一次调用的成本（未收录模型跳过）"""
    for prefix, (p_price, c_price) in MODEL_COST_PER_MTOKEN:
        if model.startswith(prefix):
            cost = (prompt_tokens * p_price + completion_tokens * c_price) / 1_000_000
            if cost > 0:
                LLM_COST_ESTIMATED_TOTAL.labels(agent=agent, model=model).inc(cost)
            return
AGENT_STAGE_DURATION = Histogram(
    "rca_agent_stage_duration_seconds",
    "Agent 单阶段耗时（秒）",
    labelnames=("agent", "mode"),  # mode: llm | rule
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0),
    registry=REGISTRY,
)
HITL_DECISIONS_TOTAL = Counter(
    "rca_hitl_decisions_total",
    "HITL 人工决策次数",
    labelnames=("gate", "action"),
    registry=REGISTRY,
)
JTI_FAILOPEN_TOTAL = Counter(
    "rca_jti_failopen_total",
    "jti 黑名单降级放行次数（Redis 不可用/操作失败，吊销未真正生效）",
    labelnames=("op",),  # op: revoke | check
    registry=REGISTRY,
)
AUDIT_SEQ_CONFLICT_RETRIES_TOTAL = Counter(
    "rca_audit_seq_conflict_retries_total",
    "审计 seq 唯一冲突重试次数（advisory lock 兜底失效的竞态窗口）",
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
            # 用路由 path 而非完整 URL（避免高基数）；
            # 未匹配路由（扫描器打任意 path）归一化为 "unmatched"，防止 label 爆炸
            endpoint = self._get_route_path(request) or "unmatched"

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
    def _get_route_path(request: Request) -> str | None:
        """从 request.scope 提取路由模板（如 /cases/{case_id}）"""
        route = request.scope.get("route")
        if route and hasattr(route, "path"):
            return str(route.path)
        return None


def _render_metrics() -> bytes:
    """单进程返回 REGISTRY；multiprocess 模式返回全部 worker 进程的聚合。"""
    if _MULTIPROC_DIR:
        from prometheus_client import multiprocess

        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        return generate_latest(registry)
    return generate_latest(REGISTRY)


def register_metrics_endpoint(app: FastAPI, service_name: str = "") -> None:
    """注册 /metrics 端点 —— Prometheus 抓取

    service_name：与 MetricsMiddleware 一致的标签来源（不再用 app.title 造成同进程两套标签）
    """

    @app.get("/metrics", tags=["meta"], include_in_schema=False)
    async def metrics():
        """Prometheus metrics 端点"""
        # 更新 DB 连接池指标
        svc = service_name or app.title
        try:
            from shared.database import engine
            pool = engine.pool
            if isinstance(pool, QueuePool):
                DB_POOL_SIZE.labels(service=svc).set(pool.size())
                DB_POOL_CHECKED_OUT.labels(service=svc).set(pool.checkedout())
        except Exception:
            pass

        return Response(
            content=_render_metrics(),
            media_type=CONTENT_TYPE_LATEST,
        )
