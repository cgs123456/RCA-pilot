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
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from .error_codes import ErrorCode


def _client_ip(request: Request) -> str:
    """获取真实客户端 IP（穿透代理）"""
    # X-Forwarded-For: client, proxy1, proxy2
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    # X-Real-IP（Nginx 设置）
    xri = request.headers.get("X-Real-IP")
    if xri:
        return xri
    return get_remote_address(request)


# 全局 Limiter 实例 —— 默认用 IP 限流
limiter = Limiter(key_func=_client_ip)


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
                "trace_id": request.headers.get("X-Trace-Id", ""),
            },
            headers={
                "Retry-After": "60",
                "X-Trace-Id": request.headers.get("X-Trace-Id", ""),
            },
        )


# ============================================================
# 预定义限流装饰器 —— 端点直接使用
# ============================================================
login_limit = limiter.limit("10/minute")  # /auth/login 防爆破
refresh_limit = limiter.limit("20/minute")  # /auth/refresh
api_limit = limiter.limit("100/second")  # 普通 API
write_limit = limiter.limit("30/minute")  # 写操作更严格（防滥用）
