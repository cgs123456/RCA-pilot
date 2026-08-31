"""slowapi 限流中间件 —— 大厂标准

策略：
- /auth/login: 10 次/分钟/IP（防爆破）
- /auth/refresh: 20 次/分钟/IP
- 其他 API: 100 次/秒/IP（防爬虫）
- /metrics, /health, /ready: 不限流
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded

from .error_codes import ErrorCode
from .error_handler import get_trace_id


def _client_ip(request: Request) -> str:
    """获取真实客户端 IP —— 与 auth_middleware.get_client_ip 保持同一可信代理策略"""
    from .auth_middleware import get_client_ip

    return get_client_ip(request)


# 全局 Limiter 实例 —— 默认用 IP 限流
# 存储后端：配置了 Redis 用 Redis（多进程/多副本共享计数），
# 否则用进程内存（单机开发模式；多 worker 下限流各自独立，仅兜底）
def _build_limiter() -> Limiter:
    from .config import settings

    if settings.env != "test":
        try:
            import redis  # noqa: F401

            return Limiter(
                key_func=_client_ip,
                storage_uri=settings.redis_url,
                in_memory_fallback_enabled=True,  # Redis 抖动时降级内存
            )
        except ImportError:
            pass
    return Limiter(key_func=_client_ip)


limiter = _build_limiter()


def init_rate_limiter(app: FastAPI) -> None:
    """初始化限流中间件 —— 在 create_app 中调用"""
    app.state.limiter = limiter

    # 注册 RateLimitExceeded 异常处理器 —— 返回统一错误格式
    @app.exception_handler(RateLimitExceeded)
    async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
        return JSONResponse(
            status_code=429,
            content={
                "code": ErrorCode.RATE_LIMITED.value,
                "message": "请求过于频繁，请稍后再试",
                "detail": str(exc.detail),
                "trace_id": get_trace_id(),
            },
            headers={
                "Retry-After": "60",
                "X-Trace-Id": get_trace_id(),
            },
        )


# ============================================================
# 预定义限流装饰器 —— 端点直接使用
# ============================================================
login_limit = limiter.limit("10/minute")  # /auth/login 防爆破
refresh_limit = limiter.limit("20/minute")  # /auth/refresh
api_limit = limiter.limit("100/second")  # 普通 API
write_limit = limiter.limit("30/minute")  # 写操作更严格（防滥用）
