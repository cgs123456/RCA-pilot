"""SQLAlchemy ORM 模型 — 对应 PostgreSQL 表结构"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class User(Base):
    """用户表 — 调查员/QA/运营/审计员/管理员"""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    email: Mapped[str] = mapped_column(String(200), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    role: Mapped[str] = mapped_column(String(30), nullable=False, index=True)  # admin|investigator|qa_reviewer|ops_manager|auditor|operator
    dept: Mapped[str | None] = mapped_column(String(50))
    title: Mapped[str | None] = mapped_column(String(100))
    avatar: Mapped[str | None] = mapped_column(String(10))
    color: Mapped[str | None] = mapped_column(String(20), default="ink")
    shift: Mapped[str | None] = mapped_column(String(20))
    cert: Mapped[str | None] = mapped_column(Text)
    password_hash: Mapped[str | None] = mapped_column(String(200))  # 内置登录用（生产对接 OIDC）
    # P3-④ TOTP 第二因子：secret 非空且 pending=False 时登录要求动态验证码
    totp_secret: Mapped[str | None] = mapped_column(String(64))
    totp_pending: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=text("false")
    )
    totp_last_step: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, server_default=text("0")
    )  # 防同窗口重放
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    failed_login_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # 锁定截止时间（时间窗自动解锁）
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
    description: Mapped[str | None] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, index=True)  # critical|major|minor
    status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)  # investigating|reviewing|closed|pending
    # ICH Q9 自动严重度升级规则——根据复杂度和RPN自动调整
    # Critical → 始终 critical
    # Major + complexity=3 → critical
    # Major + complexity=2 → major
    # Minor + complexity=3 → major
    # 否则保持原 severity
    case_type: Mapped[str | None] = mapped_column(String(100))  # EM + HVAC + 培训
    # P2-⑧ 偏差类型分级（OOS/OOT 联动基础）：process_deviation|lab_oos|lab_oot|complaint|equipment|environment|other
    deviation_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="process_deviation",
        server_default=text("'process_deviation'"), index=True,
    )
    # P2-⑧ 批次影响评估：受影响批号列表（生产/检验记录可按批号追溯）
    # server_default 不带 ::jsonb cast —— 字面量由 PG 按列类型自动转换，且 SQLite 测试库 create_all 可直接渲染
    affected_batches: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'")
    )
    complexity: Mapped[int] = mapped_column(Integer, default=1)  # 1-3
    location: Mapped[str | None] = mapped_column(String(200))
    area: Mapped[str | None] = mapped_column(String(200))
    shift: Mapped[str | None] = mapped_column(String(50))
    investigator_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("users.id"))
    qa_reviewer_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("users.id"))
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    organism: Mapped[str | None] = mapped_column(String(100))
    organism_source: Mapped[str | None] = mapped_column(String(200))
    product_impact: Mapped[str | None] = mapped_column(Text)
    immediate_action: Mapped[str | None] = mapped_column(Text)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # 结案时间（KPI 实算用）
    hitl_gates: Mapped[dict[str, str]] = mapped_column(JSONB, default=dict)  # {gate1: pending|done|rejected, ...}
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # ICH Q9 风险矩阵 —— FMEA/RPN (风险优先数)
    risk_rpn: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 优先数（1-1000，越高风险越大）
    risk_category: Mapped[str | None] = mapped_column(String(20), nullable=True)  # critical|high|medium|low
    risk_owner: Mapped[str | None] = mapped_column(String(32), ForeignKey("users.id"), nullable=True)  # 风险责任人
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    investigator: Mapped[User | None] = relationship("User", foreign_keys=[investigator_id], lazy="selectin")
    qa_reviewer: Mapped[User | None] = relationship("User", foreign_keys=[qa_reviewer_id], lazy="selectin")
    # 重型集合默认懒加载（列表/统计查询不再级联拉取 raw_data/embedding 大字段），
    # 需要的查询点用 selectinload 显式加载
    evidence: Mapped[list[Evidence]] = relationship("Evidence", back_populates="case", lazy="select", cascade="all, delete-orphan")
    timeline: Mapped[list[TimelineEvent]] = relationship("TimelineEvent", back_populates="case", lazy="select", cascade="all, delete-orphan", order_by="TimelineEvent.occurred_at")
    five_whys: Mapped[list[FiveWhy]] = relationship("FiveWhy", back_populates="case", lazy="select", cascade="all, delete-orphan", order_by="FiveWhy.n")
    skeptic_challenges: Mapped[list[SkepticChallenge]] = relationship("SkepticChallenge", back_populates="case", lazy="select", cascade="all, delete-orphan")
    capas: Mapped[list[Capa]] = relationship("Capa", back_populates="case", lazy="select", cascade="all, delete-orphan")  # type: ignore[misc]
    reports: Mapped[list[Report]] = relationship("Report", back_populates="case", lazy="select", cascade="all, delete-orphan")  # type: ignore[misc]

    __table_args__ = (
        Index("ix_deviations_status_severity", "status", "severity"),
        Index("ix_deviations_investigator", "investigator_id"),
        Index("ix_deviations_discovered_at", "discovered_at"),
    )


class Evidence(Base):
    """证据表 — 跨源系统归一化"""

    __tablename__ = "evidence"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)  # E-EMS-05
    case_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("deviations.id"), index=True)
    source_system: Mapped[str] = mapped_column(String(10), nullable=False, index=True)  # LIMS|MES|EMS|CMMS|LMS|BMS
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    raw_data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    content_hash: Mapped[str] = mapped_column(String(80), nullable=False)  # sha256:xxx
    confidence: Mapped[int] = mapped_column(Integer, default=3)  # 1-4
    is_signed: Mapped[bool] = mapped_column(Boolean, default=False)
    # 语义检索字段（Phase 2）—— embedding JSONB 是 source of truth；
    # embedding_vec（pgvector，20261025_0006 建列+回填）是检索副本，embed 时双写。
    # 无维度 vector 列：多 provider 维度不一（GLM 2048/Qwen 1024/OpenAI 1536），
    # 固定维度列写不进；检索 SQL 里用 vector_dims 过滤保证不混维比较。
    embedding: Mapped[list[float] | None] = mapped_column(JSONB, nullable=True)
    embedding_vec: Mapped[list[float] | None] = mapped_column(Vector, nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    embedded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    case: Mapped[Deviation | None] = relationship("Deviation", back_populates="evidence")

    __table_args__ = (
        CheckConstraint("confidence BETWEEN 1 AND 4", name="evidence_confidence_chk"),
        Index("ix_evidence_source_case", "source_system", "case_id"),
        Index("ix_evidence_collected_at", "collected_at"),
    )


class TimelineEvent(Base):
    """证据时间线事件"""

    __tablename__ = "timeline_events"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(32), ForeignKey("deviations.id"), index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)  # LIMS|MES|...|—
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(String(20), default="info")  # info|warning|danger
    evidence_id: Mapped[str | None] = mapped_column(String(50), ForeignKey("evidence.id"))
    is_anomaly: Mapped[bool] = mapped_column(Boolean, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    case: Mapped[Deviation] = relationship("Deviation", back_populates="timeline")

    __table_args__ = (
        Index("ix_timeline_occurred_at", "occurred_at"),
    )


class FiveWhy(Base):
    """5 Whys 推理链"""

    __tablename__ = "five_whys"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(32), ForeignKey("deviations.id"), index=True)
    n: Mapped[int] = mapped_column(Integer, nullable=False)  # 1-5
    question: Mapped[str | None] = mapped_column(Text)
    answer: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|accepted|rejected|edited
    evidence_ids: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    is_root_cause: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_model: Mapped[str | None] = mapped_column(String(100))
    ai_output_hash: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    case: Mapped[Deviation] = relationship("Deviation", back_populates="five_whys")

    __table_args__ = (
        UniqueConstraint("case_id", "n", name="five_whys_case_n_uniq"),
    )


class SkepticChallenge(Base):
    """Skeptic 反例挑战"""

    __tablename__ = "skeptic_challenges"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(32), ForeignKey("deviations.id"), index=True)
    target: Mapped[str] = mapped_column(Text, nullable=False)  # 被挑战的结论
    challenge: Mapped[str] = mapped_column(Text, nullable=False)  # Skeptic 质疑
    evidence_ids: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|accepted|rejected
    resolution: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    case: Mapped[Deviation] = relationship("Deviation", back_populates="skeptic_challenges")


class AuditLog(Base):
    """审计日志 — 不可篡改（应用层 + DB 触发器双重防护）

    hash 链：current_hash 覆盖全部字段（含 ip/session/ai_*/trace_id），
    链尾写入通过 PostgreSQL advisory lock 串行化。
    """

    __tablename__ = "audit_log"

    log_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)  # 全局自增
    operator: Mapped[str] = mapped_column(String(100), nullable=False)  # 用户邮箱或 "Evidence Hunter (AI)"
    action: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    target: Mapped[str | None] = mapped_column(String(200))
    detail: Mapped[str | None] = mapped_column(Text)
    ip_address: Mapped[str | None] = mapped_column(String(50))
    session_id: Mapped[str | None] = mapped_column(String(80))
    evidence_refs: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    ai_model: Mapped[str | None] = mapped_column(String(100))
    ai_prompt_hash: Mapped[str | None] = mapped_column(String(80))
    ai_output_hash: Mapped[str | None] = mapped_column(String(80))
    trace_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    prev_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    current_hash: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False, index=True)

    __table_args__ = (
        Index("ix_audit_operator_created", "operator", "created_at"),
    )

    # 注意：DB 层防篡改触发器在 init SQL 与 alembic 迁移中创建（阻止 UPDATE/DELETE/TRUNCATE）


class AgentRun(Base):
    """Agent 流水线运行记录"""

    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)  # RUN-xxx
    case_id: Mapped[str] = mapped_column(String(32), ForeignKey("deviations.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|running|paused|cancelled|completed|failed
    current_agent: Mapped[str | None] = mapped_column(String(30))
    error_message: Mapped[str | None] = mapped_column(Text)  # 失败/取消原因（GxP 追溯）
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    agent_states: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)  # 每个 Agent 的状态/输出
    triggered_by: Mapped[str] = mapped_column(String(100))  # 用户邮箱
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        Index("ix_agentruns_status", "status"),
        # DB 级兜底：同案例同时只允许一条活跃流水线（与应用层 ACTIVE_RUN_STATUSES 对齐，
        # 防止查重 SELECT 与 INSERT 之间的并发竞态；WHERE 子句与 models 注释同步维护）
        Index(
            "uq_agentruns_one_active_per_case",
            "case_id",
            unique=True,
            sqlite_where=text("status IN ('pending', 'running')"),
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
    )


class SourceSystem(Base):
    """源系统注册表"""

    __tablename__ = "source_systems"

    code: Mapped[str] = mapped_column(String(10), primary_key=True)  # LIMS
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    icon: Mapped[str] = mapped_column(String(50), default="database")
    color: Mapped[str] = mapped_column(String(30), default="src-lims")
    description: Mapped[str | None] = mapped_column(Text)
    connector_url: Mapped[str | None] = mapped_column(String(200))
    health_status: Mapped[str] = mapped_column(String(20), default="unknown")  # healthy|degraded|down|unknown
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
    certificate_fingerprint: Mapped[str | None] = mapped_column(String(80))
    ip_address: Mapped[str | None] = mapped_column(String(50))
    confirmation_text: Mapped[str] = mapped_column(Text, default="我确认本电子签名等同于手写签名，所提交内容真实准确")
    # 受控修订轮次（reopen 计数）：0=首次签署；结案重审后重新签署属新修订，
    # 唯一约束含 revision 使同一签字人在不同修订轮次可再次签署（旧签名记录不可篡改地保留）
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    __table_args__ = (
        # 同一用户对同一案例同一含义同一修订轮次只能签一次（Part 11 签名唯一性）
        UniqueConstraint("case_id", "signature_meaning", "signer_id", "revision", name="signatures_case_meaning_signer_uniq"),
    )


class Capa(Base):
    """CAPA (纠正预防措施) — GxP 合规必备

    支持全生命周期：描述→评审→实施→验证→闭环
    关联偏差案例，记录责任人、截止日期、有效性检查
    """

    __tablename__ = "capas"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    # case_id 不设 index=True —— __table_args__ 已有显式 Index("ix_capas_case_id")，
    # 两者同名会令 create_all 对同一索引创建两次而报 "index already exists"。
    case_id: Mapped[str] = mapped_column(String(32), ForeignKey("deviations.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)  # CAPA 标题/描述
    description: Mapped[str | None] = mapped_column(Text)  # 详细描述
    severity: Mapped[str] = mapped_column(String(20), nullable=False)  # critical|major|minor|info
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")  # pending|under_review|implemented|verified|closed
    responsibility: Mapped[str] = mapped_column(String(100), nullable=False)  # 责任人角色
    due_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # 处理期限
    verified_by: Mapped[str | None] = mapped_column(String(32), ForeignKey("users.id"))  # 验证人
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # 验证时间
    effectiveness_rate: Mapped[float | None] = mapped_column(Integer, default=None)  # 有效性评分 0-100
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    # 关系
    case: Mapped[Deviation | None] = relationship("Deviation", back_populates="capas", lazy="select")  # type: ignore[misc]

    __table_args__ = (
        Index("ix_capas_case_id", "case_id"),
        Index("ix_capas_status", "status"),
        Index("ix_capas_due_date", "due_date"),
    )


class Report(Base):
    """ICH Q9 十节格式报告 — 版本化管理

    每次 HITL 关卡编辑或签名变更时生成新版本，
    支持回溯和审计追踪，符合 GxP 文件控制要求
    """

    __tablename__ = "reports"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    # case_id 不设 index=True —— __table_args__ 已有显式 Index("ix_reports_case_id")，
    # 两者同名会令 create_all 对同一索引创建两次而报 "index already exists"。
    case_id: Mapped[str] = mapped_column(String(32), ForeignKey("deviations.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)  # 版本号，每次变更 +1
    title: Mapped[str] = mapped_column(String(500), nullable=False)  # 报告标题
    content: Mapped[str] = mapped_column(Text, nullable=False)  # Markdown 内容（含 ```markdown ``` 围栏）
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="draft")  # draft|under_review|approved|rejected
    author_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id"), nullable=False)  # 作者
    approved_by: Mapped[str | None] = mapped_column(String(32), ForeignKey("users.id"))  # 批准人
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # 批准时间
    change_reason: Mapped[str | None] = mapped_column(Text)  # 变更原因（用于 diff 对比）
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    # 关系
    case: Mapped[Deviation | None] = relationship("Deviation", back_populates="reports", lazy="select")  # type: ignore[misc]
    author: Mapped[User | None] = relationship("User", foreign_keys=[author_id])  # type: ignore[attr-defined]
    approver: Mapped[User | None] = relationship("User", foreign_keys=[approved_by])  # type: ignore[attr-defined]

    __table_args__ = (
        UniqueConstraint("case_id", "version", name="reports_case_version_uniq"),
        Index("ix_reports_case_id", "case_id"),
        Index("ix_reports_status", "status"),
    )


class Notification(Base):
    """P2-⑧ 站内协作通知 — 最小闭环（ assignment / 结案 / CAPA / 关卡就绪）"""

    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # NTF-{uuid12}
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id"), nullable=False)
    case_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("deviations.id"))
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    # CASE_ASSIGNED|CASE_CLOSED|CAPA_CREATED|GATE_READY
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        Index("ix_notifications_user_created", "user_id", "created_at"),
        # 未读徽章计数热路径；postgresql_where 在 SQLite（测试库）被忽略为普通索引
        Index(
            "ix_notifications_unread",
            "user_id",
            postgresql_where=text("read_at IS NULL"),
        ),
    )
