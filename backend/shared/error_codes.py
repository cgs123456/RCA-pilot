"""错误码枚举 —— 统一错误标识

大厂标准：每个错误有唯一 code，前端按 code 做不同处理（重试/跳转/提示）。
"""
from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    """错误码枚举 —— 前端按 code 分支处理"""

    # ============================================================
    # 通用错误 4xxx
    # ============================================================
    INTERNAL_ERROR = "E5000"  # 内部异常
    SERVICE_UNAVAILABLE = "E5001"  # 服务不可用
    RATE_LIMITED = "E4290"  # 限流
    VALIDATION_ERROR = "E4220"  # 参数校验失败（pydantic 格式错误）
    BUSINESS_VALIDATION_ERROR = "E4000"  # 业务校验失败（如状态机违规）
    NOT_FOUND = "E4040"  # 资源不存在
    METHOD_NOT_ALLOWED = "E4050"

    # ============================================================
    # 认证/授权 1xxx
    # ============================================================
    UNAUTHORIZED = "E4010"  # 未登录
    TOKEN_EXPIRED = "E4011"  # token 过期
    TOKEN_INVALID = "E4012"  # token 无效
    FORBIDDEN = "E4030"  # 无权限
    ROLE_INSUFFICIENT = "E4031"  # 角色不足
    ACCOUNT_LOCKED = "E4013"  # 账号锁定
    ACCOUNT_DISABLED = "E4014"  # 账号禁用
    MFA_REQUIRED = "E4015"  # 需要 TOTP 动态验证码
    MFA_CODE_INVALID = "E4016"  # TOTP 动态验证码错误

    # ============================================================
    # 业务错误 2xxx
    # ============================================================
    CASE_NOT_FOUND = "E2001"
    CASE_ALREADY_CLOSED = "E2002"  # 案例已结案不可修改
    CASE_STATUS_TRANSITION_INVALID = "E2003"  # 非法状态流转（如 PATCH 直接关案）
    HITL_GATE_INVALID = "E2004"  # HITL 关卡无效
    HITL_GATE_ALREADY_DONE = "E2005"  # HITL 关卡已完成
    SIGNATURE_REQUIRED = "E2006"  # 需要电子签名
    SIGNATURE_DUAL_REQUIRED = "E2007"  # 需要双人签名
    SIGNATURE_PASSWORD_WRONG = "E2008"  # 签名密码错误

    # ============================================================
    # GxP 合规错误 3xxx
    # ============================================================
    AUDIT_HASH_BROKEN = "E3001"  # 审计 hash 链断裂
    AUDIT_WRITE_FAILED = "E3002"  # 审计日志写入失败
    EVIDENCE_HASH_MISMATCH = "E3003"  # 证据 hash 不匹配
    AI_HALLUCINATION_DETECTED = "E3004"  # AI 幻觉检测
    ALCOA_VIOLATION = "E3005"  # ALCOA+ 违规

    # ============================================================
    # Agent 错误 4xxx（业务子类）
    # ============================================================
    AGENT_PIPELINE_FAILED = "E4001"
    AGENT_TIMEOUT = "E4002"
    AGENT_PAUSED = "E4003"

    # ============================================================
    # 连接器错误 5xxx
    # ============================================================
    CONNECTOR_TIMEOUT = "E5002"
    CONNECTOR_UNREACHABLE = "E5003"
    SOURCE_SYSTEM_READONLY = "E5004"


# HTTP 状态码映射
ERROR_HTTP_STATUS = {
    ErrorCode.INTERNAL_ERROR: 500,
    ErrorCode.SERVICE_UNAVAILABLE: 503,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.VALIDATION_ERROR: 422,
    ErrorCode.BUSINESS_VALIDATION_ERROR: 400,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.METHOD_NOT_ALLOWED: 405,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.TOKEN_EXPIRED: 401,
    ErrorCode.TOKEN_INVALID: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.ROLE_INSUFFICIENT: 403,
    ErrorCode.ACCOUNT_LOCKED: 401,
    ErrorCode.ACCOUNT_DISABLED: 401,
    ErrorCode.MFA_REQUIRED: 401,
    ErrorCode.MFA_CODE_INVALID: 401,
    ErrorCode.CASE_NOT_FOUND: 404,
    ErrorCode.CASE_ALREADY_CLOSED: 400,
    ErrorCode.CASE_STATUS_TRANSITION_INVALID: 400,
    ErrorCode.HITL_GATE_INVALID: 400,
    ErrorCode.HITL_GATE_ALREADY_DONE: 400,
    ErrorCode.SIGNATURE_REQUIRED: 400,
    ErrorCode.SIGNATURE_DUAL_REQUIRED: 400,
    ErrorCode.SIGNATURE_PASSWORD_WRONG: 401,
    ErrorCode.AUDIT_HASH_BROKEN: 500,
    ErrorCode.AUDIT_WRITE_FAILED: 500,
    ErrorCode.EVIDENCE_HASH_MISMATCH: 500,
    ErrorCode.AI_HALLUCINATION_DETECTED: 500,
    ErrorCode.ALCOA_VIOLATION: 400,
    ErrorCode.AGENT_PIPELINE_FAILED: 500,
    ErrorCode.AGENT_TIMEOUT: 504,
    ErrorCode.AGENT_PAUSED: 409,
    ErrorCode.CONNECTOR_TIMEOUT: 504,
    ErrorCode.CONNECTOR_UNREACHABLE: 502,
    ErrorCode.SOURCE_SYSTEM_READONLY: 400,
}
