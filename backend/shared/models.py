"""SQLAlchemy ORM 模型 — 对应 PostgreSQL 表结构"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    """用户表 — 调查员/QA/运营/审计员/管理员"""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    email: Mapped[str] = mapped_column(String(200), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    role: Mapped[str] = mapped_column(String(30), nullable=False, index=True)  # admin|investigator|qa_reviewer|ops_manager|auditor|operator
    dept: Mapped[Optional[str]] = mapped_column(String(50))
    title: Mapped[Optional[str]] = mapped_column(String(100))
    avatar: Mapped[Optional[str]] = mapped_column(String(10))
    color: Mapped[Optional[str]] = mapped_column(String(20), default="ink")
    shift: Mapped[Optional[str]] = mapped_column(String(20))
    cert: Mapped[Optional[str]] = mapped_column(Text)
    password_hash: Mapped[Optional[str]] = mapped_column(String(200))  # 内置登录用（生产对接 OIDC）
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    failed_login_attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        # 应用层不再声明 Python 端 CHECK（DB 层 01_schema.sql 已有 role IN (...) 约束）。
        # 此处 Python 表达式求值结果不可靠（role 在类定义时未绑定），保留会恒为 False 或恒为 True。
    )


class Deviation(Base):
    """偏差案例表"""

    __tablename__ = "deviations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # DEV-2025-08-147
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, index=True)  # critical|major|minor
    status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)  # investigating|reviewing|closed|pending
    case_type: Mapped[Optional[str]] = mapped_column(String(100))  # EM + HVAC + 培训
    complexity: Mapped[int] = mapped_column(Integer, default=1)  # 1-3
    location: Mapped[Optional[str]] = mapped_column(String(200))
    area: Mapped[Optional[str]] = mapped_column(String(200))
    shift: Mapped[Optional[str]] = mapped_column(String(50))
    investigator_id: Mapped[Optional[str]] = mapped_column(String(32), ForeignKey("users.id"))
    qa_reviewer_id: Mapped[Optional[str]] = mapped_column(String(32), ForeignKey("users.id"))
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deadline: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    organism: Mapped[Optional[str]] = mapped_column(String(100))
    organism_source: Mapped[Optional[str]] = mapped_column(String(200))
    product_impact: Mapped[Optional[str]] = mapped_column(Text)
    immediate_action: Mapped[Optional[str]] = mapped_column(Text)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    hitl_gates: Mapped[Dict[str, str]] = mapped_column(JSONB, default=dict)  # {gate1: pending|done|rejected, ...}
    meta: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    investigator: Mapped[Optional["User"]] = relationship("User", foreign_keys=[investigator_id], lazy="selectin")
    qa_reviewer: Mapped[Optional["User"]] = relationship("User", foreign_keys=[qa_reviewer_id], lazy="selectin")
    evidence: Mapped[List["Evidence"]] = relationship("Evidence", back_populates="case", lazy="selectin", cascade="all, delete-orphan")
    timeline: Mapped[List["TimelineEvent"]] = relationship("TimelineEvent", back_populates="case", lazy="selectin", cascade="all, delete-orphan", order_by="TimelineEvent.occurred_at")
    five_whys: Mapped[List["FiveWhy"]] = relationship("FiveWhy", back_populates="case", lazy="selectin", cascade="all, delete-orphan", order_by="FiveWhy.n")
    skeptic_challenges: Mapped[List["SkepticChallenge"]] = relationship("SkepticChallenge", back_populates="case", lazy="selectin", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_deviations_status_severity", "status", "severity"),
        Index("ix_deviations_investigator", "investigator_id"),
    )


class Evidence(Base):
    """证据表 — 跨源系统归一化"""

    __tablename__ = "evidence"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)  # E-EMS-05
    case_id: Mapped[Optional[str]] = mapped_column(String(32), ForeignKey("deviations.id"), index=True)
    source_system: Mapped[str] = mapped_column(String(10), nullable=False, index=True)  # LIMS|MES|EMS|CMMS|LMS|BMS
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    summary: Mapped[Optional[str]] = mapped_column(Text)
    raw_data: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    content_hash: Mapped[str] = mapped_column(String(80), nullable=False)  # sha256:xxx
    confidence: Mapped[int] = mapped_column(Integer, default=3)  # 1-4
    is_signed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    case: Mapped[Optional["Deviation"]] = relationship("Deviation", back_populates="evidence")

    __table_args__ = (
        CheckConstraint("confidence BETWEEN 1 AND 4", name="evidence_confidence_chk"),
        Index("ix_evidence_source_case", "source_system", "case_id"),
    )


class TimelineEvent(Base):
    """证据时间线事件"""

    __tablename__ = "timeline_events"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(32), ForeignKey("deviations.id"), index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)  # LIMS|MES|...|—
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    detail: Mapped[Optional[str]] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(String(20), default="info")  # info|warning|danger
    evidence_id: Mapped[Optional[str]] = mapped_column(String(50), ForeignKey("evidence.id"))
    is_anomaly: Mapped[bool] = mapped_column(Boolean, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    case: Mapped["Deviation"] = relationship("Deviation", back_populates="timeline")


class FiveWhy(Base):
    """5 Whys 推理链"""

    __tablename__ = "five_whys"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(32), ForeignKey("deviations.id"), index=True)
    n: Mapped[int] = mapped_column(Integer, nullable=False)  # 1-5
    question: Mapped[Optional[str]] = mapped_column(Text)
    answer: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|accepted|rejected|edited
    evidence_ids: Mapped[List[str]] = mapped_column(ARRAY(String), default=list)
    is_root_cause: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_model: Mapped[Optional[str]] = mapped_column(String(100))
    ai_output_hash: Mapped[Optional[str]] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    case: Mapped["Deviation"] = relationship("Deviation", back_populates="five_whys")


class SkepticChallenge(Base):
    """Skeptic 反例挑战"""

    __tablename__ = "skeptic_challenges"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(32), ForeignKey("deviations.id"), index=True)
    target: Mapped[str] = mapped_column(Text, nullable=False)  # 被挑战的结论
    challenge: Mapped[str] = mapped_column(Text, nullable=False)  # Skeptic 质疑
    evidence_ids: Mapped[List[str]] = mapped_column(ARRAY(String), default=list)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|accepted|rejected
    resolution: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    case: Mapped["Deviation"] = relationship("Deviation", back_populates="skeptic_challenges")


class AuditLog(Base):
    """审计日志 — 不可篡改（应用层 + DB RULE 双重防护）

    hash 链：current_hash = sha256(prev_hash + seq + operator + action + target + detail + ts + evidence_refs)
    """

    __tablename__ = "audit_log"

    log_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)  # 全局自增
    operator: Mapped[str] = mapped_column(String(100), nullable=False)  # 用户邮箱或 "Evidence Hunter (AI)"
    action: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    target: Mapped[Optional[str]] = mapped_column(String(200))
    detail: Mapped[Optional[str]] = mapped_column(Text)
    ip_address: Mapped[Optional[str]] = mapped_column(String(50))
    session_id: Mapped[Optional[str]] = mapped_column(String(80))
    evidence_refs: Mapped[List[str]] = mapped_column(ARRAY(String), default=list)
    ai_model: Mapped[Optional[str]] = mapped_column(String(100))
    ai_prompt_hash: Mapped[Optional[str]] = mapped_column(String(80))
    ai_output_hash: Mapped[Optional[str]] = mapped_column(String(80))
    prev_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    current_hash: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False, index=True)

    # 注意：DB 层防篡改 RULE 在 init SQL 中创建（CREATE RULE ... DO INSTEAD NOTHING）


class AgentRun(Base):
    """Agent 流水线运行记录"""

    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)  # RUN-xxx
    case_id: Mapped[str] = mapped_column(String(32), ForeignKey("deviations.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|running|paused|completed|failed
    current_agent: Mapped[Optional[str]] = mapped_column(String(30))
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    agent_states: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)  # 每个 Agent 的状态/输出
    triggered_by: Mapped[str] = mapped_column(String(100))  # 用户邮箱
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class SourceSystem(Base):
    """源系统注册表"""

    __tablename__ = "source_systems"

    code: Mapped[str] = mapped_column(String(10), primary_key=True)  # LIMS
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    icon: Mapped[str] = mapped_column(String(50), default="database")
    color: Mapped[str] = mapped_column(String(30), default="src-lims")
    description: Mapped[Optional[str]] = mapped_column(Text)
    connector_url: Mapped[Optional[str]] = mapped_column(String(200))
    health_status: Mapped[str] = mapped_column(String(20), default="unknown")  # healthy|degraded|down|unknown
    last_sync_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    is_readonly: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class SignatureRecord(Base):
    """电子签名记录 — 21 CFR Part 11"""

    __tablename__ = "signatures"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(32), ForeignKey("deviations.id"), index=True)
    signer_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id"), nullable=False)
    signer_role: Mapped[str] = mapped_column(String(30), nullable=False)  # investigator|qa_reviewer
    signature_meaning: Mapped[str] = mapped_column(String(100), nullable=False)  # 提交签名|批准签名
    signed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    certificate_fingerprint: Mapped[Optional[str]] = mapped_column(String(80))
    ip_address: Mapped[Optional[str]] = mapped_column(String(50))
    confirmation_text: Mapped[str] = mapped_column(Text, default="我确认本电子签名等同于手写签名，所提交内容真实准确")
