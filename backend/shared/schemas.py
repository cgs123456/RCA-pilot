"""Pydantic schemas — API 请求/响应模型"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Generic, List, Literal, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, EmailStr, Field


# ============================================================
#  通用
# ============================================================
T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """分页响应 — 支持泛型参数化 Page[ItemType]"""
    items: List[T]
    total: int
    page: int = 1
    page_size: int = 20


class APIError(BaseModel):
    """错误响应"""
    code: str
    message: str
    details: Optional[Dict[str, Any]] = None
    trace_id: Optional[str] = None


# ============================================================
#  认证
# ============================================================
class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6, max_length=128)
    mfa_code: Optional[str] = Field(default=None, min_length=6, max_length=6)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: "UserOut"


class RefreshRequest(BaseModel):
    refresh_token: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    email: str
    name: str
    role: str
    dept: Optional[str] = None
    title: Optional[str] = None
    avatar: Optional[str] = None
    color: Optional[str] = None
    shift: Optional[str] = None
    cert: Optional[str] = None


# ============================================================
#  偏差案例
# ============================================================
Severity = Literal["critical", "major", "minor", "info"]
CaseStatus = Literal["pending", "investigating", "reviewing", "closed"]
HitlGateStatus = Literal["pending", "done", "rejected"]


class DeviationBase(BaseModel):
    title: str = Field(min_length=5, max_length=500)
    description: Optional[str] = None
    severity: Severity = "minor"
    case_type: Optional[str] = None
    complexity: int = Field(default=1, ge=1, le=3)
    location: Optional[str] = None
    area: Optional[str] = None
    shift: Optional[str] = None
    investigator_id: Optional[str] = None
    qa_reviewer_id: Optional[str] = None
    discovered_at: datetime
    deadline: Optional[datetime] = None
    organism: Optional[str] = None
    organism_source: Optional[str] = None
    product_impact: Optional[str] = None
    immediate_action: Optional[str] = None
    meta: Dict[str, Any] = Field(default_factory=dict)


class DeviationCreate(DeviationBase):
    id: Optional[str] = None  # 可由后端自动生成


class DeviationUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    severity: Optional[Severity] = None
    status: Optional[CaseStatus] = None
    investigator_id: Optional[str] = None
    qa_reviewer_id: Optional[str] = None
    deadline: Optional[datetime] = None
    immediate_action: Optional[str] = None
    progress: Optional[int] = Field(default=None, ge=0, le=100)
    meta: Optional[Dict[str, Any]] = None


class DeviationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    title: str
    description: Optional[str] = None
    severity: Severity
    status: CaseStatus
    case_type: Optional[str] = None
    complexity: int = 1
    location: Optional[str] = None
    area: Optional[str] = None
    shift: Optional[str] = None
    investigator_id: Optional[str] = None
    qa_reviewer_id: Optional[str] = None
    discovered_at: datetime
    deadline: Optional[datetime] = None
    organism: Optional[str] = None
    organism_source: Optional[str] = None
    product_impact: Optional[str] = None
    immediate_action: Optional[str] = None
    progress: int = 0
    hitl_gates: Dict[str, str] = Field(default_factory=dict)
    meta: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    # 关联
    investigator: Optional[UserOut] = None
    qa_reviewer: Optional[UserOut] = None


class DeviationDetail(DeviationOut):
    """含证据、时间线、5Whys 等完整信息"""
    evidence: List["EvidenceOut"] = Field(default_factory=list)
    timeline: List["TimelineEventOut"] = Field(default_factory=list)
    five_whys: List["FiveWhyOut"] = Field(default_factory=list)
    skeptic_challenges: List["SkepticChallengeOut"] = Field(default_factory=list)


# ============================================================
#  证据
# ============================================================
SourceSystem = Literal["LIMS", "MES", "EMS", "CMMS", "LMS", "BMS"]


class EvidenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    case_id: Optional[str] = None
    source_system: SourceSystem
    collected_at: datetime
    summary: Optional[str] = None
    raw_data: Dict[str, Any] = Field(default_factory=dict)
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
    detail: Optional[str] = None
    severity: str = "info"
    evidence_id: Optional[str] = None
    is_anomaly: bool = False
    sort_order: int = 0


class FiveWhyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    case_id: str
    n: int
    question: Optional[str] = None
    answer: Optional[str] = None
    status: str = "pending"
    evidence_ids: List[str] = Field(default_factory=list)
    is_root_cause: bool = False
    ai_model: Optional[str] = None
    ai_output_hash: Optional[str] = None


class SkepticChallengeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    case_id: str
    target: str
    challenge: str
    evidence_ids: List[str] = Field(default_factory=list)
    status: str = "pending"
    resolution: Optional[str] = None
    created_at: datetime
    resolved_at: Optional[datetime] = None


# ============================================================
#  HITL 关卡
# ============================================================
HitlAction = Literal["accept", "reject", "edit"]


class HitlGateRequest(BaseModel):
    gate_id: str = Field(..., pattern=r"^gate[1-5]$")
    action: HitlAction
    comment: Optional[str] = None
    edit_payload: Optional[Dict[str, Any]] = None


class HitlGateResponse(BaseModel):
    case_id: str
    gate_id: str
    action: HitlAction
    new_status: str
    all_gates_passed: bool
    triggered_agent: Optional[str] = None  # 例如 gate3 accept 触发 Reasoner 重新推理


# ============================================================
#  审计日志
# ============================================================
class AuditLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    log_id: str
    seq: int
    operator: str
    action: str
    target: Optional[str] = None
    detail: Optional[str] = None
    ip_address: Optional[str] = None
    session_id: Optional[str] = None
    evidence_refs: List[str] = Field(default_factory=list)
    ai_model: Optional[str] = None
    ai_output_hash: Optional[str] = None
    prev_hash: str
    current_hash: str
    created_at: datetime


class AuditLogCreate(BaseModel):
    """内部调用审计服务"""
    operator: str
    action: str
    target: Optional[str] = None
    detail: Optional[str] = None
    ip_address: Optional[str] = None
    session_id: Optional[str] = None
    evidence_refs: List[str] = Field(default_factory=list)
    ai_model: Optional[str] = None
    ai_prompt_hash: Optional[str] = None
    ai_output_hash: Optional[str] = None


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
    current_agent: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    agent_states: Dict[str, Any] = Field(default_factory=dict)
    triggered_by: str
    created_at: datetime


# ============================================================
#  电子签名
# ============================================================
class SignatureRequest(BaseModel):
    case_id: str
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
    certificate_fingerprint: Optional[str] = None
    ip_address: Optional[str] = None
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
    description: Optional[str] = None
    health_status: str
    last_sync_at: Optional[datetime] = None
    is_readonly: bool = True


# Forward refs
TokenResponse.model_rebuild()
DeviationDetail.model_rebuild()
