"""Pydantic schemas — API 请求/响应模型"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, EmailStr, Field

# ============================================================
#  通用
# ============================================================
T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """分页响应 — 支持泛型参数化 Page[ItemType]"""
    items: list[T]
    total: int
    page: int = 1
    page_size: int = 20


class APIError(BaseModel):
    """错误响应"""
    code: str
    message: str
    details: dict[str, Any] | None = None
    trace_id: str | None = None


# ============================================================
#  认证
# ============================================================
class LoginRequest(BaseModel):
    email: EmailStr
    # GxP 密码策略：最低 12 位（复杂度校验在 auth_service 按环境启用）
    password: str = Field(min_length=12, max_length=128)
    mfa_code: str | None = Field(default=None, min_length=6, max_length=6)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


class RefreshRequest(BaseModel):
    refresh_token: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    email: str
    name: str
    role: str
    dept: str | None = None
    title: str | None = None
    avatar: str | None = None
    color: str | None = None
    shift: str | None = None
    cert: str | None = None


# ============================================================
#  偏差案例
# ============================================================
Severity = Literal["critical", "major", "minor", "info"]
CaseStatus = Literal["pending", "investigating", "reviewing", "closed"]
HitlGateStatus = Literal["pending", "done", "rejected"]


class DeviationBase(BaseModel):
    title: str = Field(min_length=5, max_length=500)
    description: str | None = None
    severity: Severity = "minor"
    case_type: str | None = None
    complexity: int = Field(default=1, ge=1, le=3)
    location: str | None = None
    area: str | None = None
    shift: str | None = None
    investigator_id: str | None = None
    qa_reviewer_id: str | None = None
    discovered_at: datetime
    deadline: datetime | None = None
    organism: str | None = None
    organism_source: str | None = None
    product_impact: str | None = None
    immediate_action: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class DeviationCreate(DeviationBase):
    id: str | None = None  # 可由后端自动生成


class DeviationUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    severity: Severity | None = None
    status: CaseStatus | None = None
    investigator_id: str | None = None
    qa_reviewer_id: str | None = None
    deadline: datetime | None = None
    immediate_action: str | None = None
    progress: int | None = Field(default=None, ge=0, le=100)
    meta: dict[str, Any] | None = None


class DeviationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    title: str
    description: str | None = None
    severity: Severity
    status: CaseStatus
    case_type: str | None = None
    complexity: int = 1
    location: str | None = None
    area: str | None = None
    shift: str | None = None
    investigator_id: str | None = None
    qa_reviewer_id: str | None = None
    discovered_at: datetime
    deadline: datetime | None = None
    organism: str | None = None
    organism_source: str | None = None
    product_impact: str | None = None
    immediate_action: str | None = None
    progress: int = 0
    closed_at: datetime | None = None
    hitl_gates: dict[str, str] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    # 关联
    investigator: UserOut | None = None
    qa_reviewer: UserOut | None = None


class DeviationDetail(DeviationOut):
    """含证据、时间线、5Whys 等完整信息"""
    evidence: list[EvidenceOut] = Field(default_factory=list)
    timeline: list[TimelineEventOut] = Field(default_factory=list)
    five_whys: list[FiveWhyOut] = Field(default_factory=list)
    skeptic_challenges: list[SkepticChallengeOut] = Field(default_factory=list)


# ============================================================
#  证据
# ============================================================
SourceSystem = Literal["LIMS", "MES", "EMS", "CMMS", "LMS", "BMS"]


class EvidenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    case_id: str | None = None
    source_system: SourceSystem
    collected_at: datetime
    summary: str | None = None
    raw_data: dict[str, Any] = Field(default_factory=dict)
    content_hash: str
    confidence: int = 3
    is_signed: bool = False
    created_at: datetime


class TimelineEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    case_id: str
    occurred_at: datetime
    source: str
    title: str
    detail: str | None = None
    severity: str = "info"
    evidence_id: str | None = None
    is_anomaly: bool = False
    sort_order: int = 0


class FiveWhyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    case_id: str
    n: int
    question: str | None = None
    answer: str | None = None
    status: str = "pending"
    evidence_ids: list[str] = Field(default_factory=list)
    is_root_cause: bool = False
    ai_model: str | None = None
    ai_output_hash: str | None = None


class FiveWhyUpdate(BaseModel):
    """5 Whys 条目更新（HITL gate2 编辑）— 替代裸 dict，status 收敛为枚举"""
    question: str | None = None
    answer: str | None = None
    status: Literal["pending", "accepted", "rejected", "edited"] | None = None
    evidence_ids: list[str] | None = None


class SkepticResolve(BaseModel):
    """Skeptic 挑战处置 — 替代裸 dict"""
    status: Literal["accepted", "rejected"]
    resolution: str = Field(min_length=1, max_length=2000)


class SkepticChallengeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    case_id: str
    target: str
    challenge: str
    evidence_ids: list[str] = Field(default_factory=list)
    status: str = "pending"
    resolution: str | None = None
    created_at: datetime
    resolved_at: datetime | None = None


# ============================================================
#  HITL 关卡
# ============================================================
HitlAction = Literal["accept", "reject", "edit"]


class HitlGateRequest(BaseModel):
    gate_id: str = Field(..., pattern=r"^gate[1-5]$")
    action: HitlAction
    comment: str | None = None
    edit_payload: dict[str, Any] | None = None


class HitlGateResponse(BaseModel):
    case_id: str
    gate_id: str
    action: HitlAction
    new_status: str
    all_gates_passed: bool
    triggered_agent: str | None = None  # 例如 gate3 accept 触发 Reasoner 重新推理


# ============================================================
#  审计日志
# ============================================================
class AuditLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    log_id: str
    seq: int
    operator: str
    action: str
    target: str | None = None
    detail: str | None = None
    ip_address: str | None = None
    session_id: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    ai_model: str | None = None
    ai_prompt_hash: str | None = None
    ai_output_hash: str | None = None
    trace_id: str | None = None
    prev_hash: str
    current_hash: str
    created_at: datetime


class AuditLogCreate(BaseModel):
    """内部调用审计服务"""
    operator: str
    action: str
    target: str | None = None
    detail: str | None = None
    ip_address: str | None = None
    session_id: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    ai_model: str | None = None
    ai_prompt_hash: str | None = None
    ai_output_hash: str | None = None
    trace_id: str | None = None


# ============================================================
#  Agent
# ============================================================
class AgentRunCreate(BaseModel):
    case_id: str
    triggered_by: str


class AgentRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    case_id: str
    status: str
    current_agent: str | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    agent_states: dict[str, Any] = Field(default_factory=dict)
    triggered_by: str
    created_at: datetime


# ============================================================
#  电子签名
# ============================================================
class SignatureRequest(BaseModel):
    case_id: str | None = None  # 以路径参数为准，兼容旧客户端
    signature_meaning: Literal["submit", "approve"]
    confirmation_text: str = "我确认本电子签名等同于手写签名，所提交内容真实准确"
    password: str  # 二次确认


class SignatureOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    case_id: str
    signer_id: str
    signer_role: str
    signature_meaning: str
    signed_at: datetime
    certificate_fingerprint: str | None = None
    ip_address: str | None = None
    confirmation_text: str


# ============================================================
#  Dashboard / KPI
# ============================================================
class KPIOut(BaseModel):
    """Dashboard KPI 指标

    trend_xxx_delta 字段为变化量（带符号）：
      - 正数表示增加，负数表示减少
      - count 类指标单位为「个」
      - cycle 类指标单位为「天」
      - capa_overdue_rate 单位为「百分比 (0-1)」
    """
    unsolved_count: int  # 未解决案例数（个）
    major_count_this_month: int  # 本月重大偏差数（个）
    avg_cycle_days: float  # 平均闭环天数（天）
    capa_overdue_rate: float  # CAPA 逾期率（0-1，即百分比）
    trend_unsolved_delta: int  # 较上周未解决数变化（个，正=增加）
    trend_major_delta: int  # 较上月重大偏差数变化（个，正=增加）
    trend_cycle_delta: float  # 较 Q1 平均闭环天数变化（天，正=变慢）
    trend_capa_delta: float  # 较上月 CAPA 逾期率变化（百分比点，正=恶化）


class DeviationTrendPoint(BaseModel):
    date: str
    microbe: int = 0
    particle: int = 0
    impurity: int = 0
    signature: int = 0
    hvac: int = 0
    cleaning: int = 0


class HeatmapCell(BaseModel):
    room: str
    microbe: int
    particle: int
    impurity: int
    signature: int
    hvac: int
    cleaning: int


# ============================================================
#  源系统
# ============================================================
class SourceSystemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    code: str
    label: str
    icon: str
    color: str
    description: str | None = None
    health_status: str
    last_sync_at: datetime | None = None
    is_readonly: bool = True


# Forward refs
TokenResponse.model_rebuild()
DeviationDetail.model_rebuild()
