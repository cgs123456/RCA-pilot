"""统一异常体系 — GxP 场景下错误必须可追溯"""
from __future__ import annotations

from typing import Any


class RCAError(Exception):
    """RCA Pilot 业务异常基类

    error_code：结构化错误码（error_codes.ErrorCode 的 value），
    全局异常处理器优先读取它 —— 避免对文案做字符串匹配。
    """

    status_code: int = 500
    code: str = "INTERNAL_ERROR"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
        error_code: str | None = None,
    ):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.details = details or {}
        self.error_code = error_code


class ValidationError(RCAError):
    status_code = 422
    code = "VALIDATION_ERROR"


class NotFoundError(RCAError):
    status_code = 404
    code = "NOT_FOUND"


class ConflictError(RCAError):
    status_code = 409
    code = "CONFLICT"


class AuthError(RCAError):
    status_code = 401
    code = "AUTH_ERROR"


class ForbiddenError(RCAError):
    status_code = 403
    code = "FORBIDDEN"


class HITLViolationError(RCAError):
    """HITL 关卡违规 — 不可跳过"""
    status_code = 409
    code = "HITL_VIOLATION"


class AuditIntegrityError(RCAError):
    """审计完整性错误 — hash 链断裂"""
    status_code = 500
    code = "AUDIT_INTEGRITY_ERROR"


class AgentHallucinationError(RCAError):
    """Agent 幻觉 — 引用不存在的证据"""
    status_code = 422
    code = "AGENT_HALLUCINATION"


class ConnectorError(RCAError):
    """源系统集成错误"""
    status_code = 502
    code = "CONNECTOR_ERROR"


class SourceSystemReadOnlyError(RCAError):
    """源系统只读违规 — 试图写入 LIMS/MES 等"""
    status_code = 403
    code = "SOURCE_READ_ONLY_VIOLATION"
