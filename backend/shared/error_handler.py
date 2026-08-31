"""全局异常处理器 + traceId 透传中间件

大厂标准：
- 每个请求分配唯一 traceId，贯穿日志/响应/审计
- 统一错误响应格式 {code, message, detail, trace_id}
- 5xx 错误自动上报 Sentry
- 不泄露内部堆栈给前端
"""
from __future__ import annotations

import uuid
from contextvars import ContextVar
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from .error_codes import ERROR_HTTP_STATUS, ErrorCode
from .exceptions import (
    AuthError,
    ForbiddenError,
    HITLViolationError,
    NotFoundError,
    RCAError,
    ValidationError,
)

# 上下文变量 —— traceId 贯穿整个请求生命周期（含异步任务）
trace_id_ctx: ContextVar[str] = ContextVar("trace_id", default="")


def get_trace_id() -> str:
    """获取当前请求的 traceId（用于日志/审计）"""
    return trace_id_ctx.get()


# 异常类 → ErrorCode 注册表 —— 新增异常子类在此登记，
# 不再依赖 isinstance 链和字符串匹配
_RCA_ERROR_CODE_MAP: dict[type, ErrorCode] = {}  # 延迟填充（见下方 _build_map）


def _build_map() -> None:
    from .exceptions import (
        AgentHallucinationError,
        AuditIntegrityError,
        ConflictError,
        ConnectorError,
        SourceSystemReadOnlyError,
    )

    _RCA_ERROR_CODE_MAP.update(
        {
            AuthError: ErrorCode.UNAUTHORIZED,
            ForbiddenError: ErrorCode.FORBIDDEN,
            NotFoundError: ErrorCode.NOT_FOUND,
            ValidationError: ErrorCode.BUSINESS_VALIDATION_ERROR,
            HITLViolationError: ErrorCode.HITL_GATE_INVALID,
            ConflictError: ErrorCode.CASE_STATUS_TRANSITION_INVALID,
            AgentHallucinationError: ErrorCode.AI_HALLUCINATION_DETECTED,
            AuditIntegrityError: ErrorCode.AUDIT_HASH_BROKEN,
            ConnectorError: ErrorCode.CONNECTOR_UNREACHABLE,
            SourceSystemReadOnlyError: ErrorCode.SOURCE_SYSTEM_READONLY,
        }
    )


_build_map()


class TraceIdMiddleware(BaseHTTPMiddleware):
    """为每个请求生成/透传 traceId

    - 优先从 X-Trace-Id 请求头读取（上游服务透传）
    - 否则生成 uuid4 hex
    - 写入 contextvar 供日志使用
    - 响应头回传 X-Trace-Id 供前端排查
    """

    async def dispatch(self, request: Request, call_next):
        trace_id = request.headers.get("X-Trace-Id") or uuid.uuid4().hex
        trace_id_ctx.set(trace_id)

        response = await call_next(request)
        response.headers["X-Trace-Id"] = trace_id
        return response


def _error_response(
    code: ErrorCode,
    message: str,
    detail: Any = None,
    http_status: int | None = None,
) -> JSONResponse:
    """构造统一错误响应"""
    status = http_status or ERROR_HTTP_STATUS.get(code, 500)
    return JSONResponse(
        status_code=status,
        content={
            "code": code.value,
            "message": message,
            "detail": detail,
            "trace_id": get_trace_id(),
        },
        headers={"X-Trace-Id": get_trace_id()},
    )


def _sanitize_validation_errors(errors: list) -> list:
    """清洗 pydantic 校验错误 —— 剔除 input（可能含密码等敏感提交值）与 url"""
    sanitized = []
    for err in errors:
        if isinstance(err, dict):
            cleaned = {k: v for k, v in err.items() if k not in ("input", "url")}
            sanitized.append(cleaned)
        else:
            sanitized.append(err)
    return sanitized


def register_exception_handlers(app: FastAPI) -> None:
    """注册全局异常处理器 —— 在 create_app 中调用"""

    @app.exception_handler(RCAError)
    async def rca_error_handler(request: Request, exc: RCAError):
        """业务异常 —— 已知错误，按错误码返回

        映射优先级：
        1. 异常自带的结构化 error_code（AuthError 子场景显式设置，
           替代旧的"中文字符串匹配"方案 —— 文案改动不再影响 API 错误码）
        2. 异常类 → ErrorCode 注册表
        """
        # 1. 结构化 error_code（异常构造时显式指定）
        exc_error_code = getattr(exc, "error_code", None)
        if exc_error_code:
            try:
                code = ErrorCode(exc_error_code)
            except ValueError:
                code = None
            else:
                return _error_response(code, str(exc), detail=getattr(exc, "details", None))

        # 2. 类 → 错误码注册表
        code = _RCA_ERROR_CODE_MAP.get(type(exc))
        if code is None:
            code = ErrorCode.INTERNAL_ERROR

        return _error_response(code, str(exc), detail=getattr(exc, "details", None))

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        """Pydantic 参数校验失败 —— 422（响应不回显提交值，防止密码等泄露）"""
        return _error_response(
            ErrorCode.VALIDATION_ERROR,
            "请求参数校验失败",
            detail=_sanitize_validation_errors(exc.errors()),
        )

    @app.exception_handler(PermissionError)
    async def permission_error_handler(request: Request, exc: PermissionError):
        """权限错误 —— 403"""
        return _error_response(ErrorCode.FORBIDDEN, str(exc) or "无权限访问")

    @app.exception_handler(Exception)
    async def generic_exception_handler(request: Request, exc: Exception):
        """兜底异常 —— 5xx，自动上报 Sentry"""
        import logging

        logger = logging.getLogger("rca.exceptions")
        logger.exception(
            "未处理异常 trace_id=%s path=%s",
            get_trace_id(),
            request.url.path,
        )

        # Sentry 自动上报（如果已初始化）
        try:
            import sentry_sdk

            sentry_sdk.capture_exception(exc)
        except ImportError:
            pass

        # 生产环境不泄露堆栈
        from .config import settings

        detail = str(exc) if settings.debug else "请联系管理员并提供 trace_id 排查"
        return _error_response(
            ErrorCode.INTERNAL_ERROR,
            "服务内部错误",
            detail=detail,
        )
