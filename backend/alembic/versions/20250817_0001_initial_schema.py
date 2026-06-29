"""Alembic 迁移脚本模板"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
# from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """初始 schema —— 从 db/01_schema.sql 移植"""
    # 用户表
    op.create_table(
        "users",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("email", sa.String(200), nullable=False, unique=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("role", sa.String(30), nullable=False),
        sa.Column("dept", sa.String(50)),
        sa.Column("title", sa.String(100)),
        sa.Column("avatar", sa.String(10)),
        sa.Column("color", sa.String(20), server_default="ink"),
        sa.Column("shift", sa.String(20)),
        sa.Column("cert", sa.Text),
        sa.Column("password_hash", sa.String(200)),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("failed_login_attempts", sa.Integer, server_default="0"),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(
            "role IN ('admin','investigator','qa_reviewer','ops_manager','auditor','operator')",
            name="users_role_chk",
        ),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_index("ix_users_role", "users", ["role"])

    # 偏差案例表
    op.create_table(
        "deviations",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("case_type", sa.String(100)),
        sa.Column("complexity", sa.Integer, server_default="1"),
        sa.Column("location", sa.String(200)),
        sa.Column("area", sa.String(200)),
        sa.Column("shift", sa.String(50)),
        sa.Column("investigator_id", sa.String(32), sa.ForeignKey("users.id")),
        sa.Column("qa_reviewer_id", sa.String(32), sa.ForeignKey("users.id")),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True)),
        sa.Column("organism", sa.String(100)),
        sa.Column("organism_source", sa.String(200)),
        sa.Column("product_impact", sa.Text),
        sa.Column("immediate_action", sa.Text),
        sa.Column("progress", sa.Integer, server_default="0"),
        sa.Column("hitl_gates", sa.dialects.postgresql.JSONB, server_default="{}"),
        sa.Column("meta", sa.dialects.postgresql.JSONB, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(
            "severity IN ('critical','major','minor','info')",
            name="deviations_severity_chk",
        ),
        sa.CheckConstraint(
            "status IN ('pending','investigating','reviewing','closed')",
            name="deviations_status_chk",
        ),
    )
    op.create_index("ix_deviations_severity", "deviations", ["severity"])
    op.create_index("ix_deviations_status", "deviations", ["status"])
    op.create_index("ix_deviations_status_severity", "deviations", ["status", "severity"])
    op.create_index("ix_deviations_investigator", "deviations", ["investigator_id"])

    # 证据表
    op.create_table(
        "evidence",
        sa.Column("id", sa.String(50), primary_key=True),
        sa.Column("case_id", sa.String(32), sa.ForeignKey("deviations.id")),
        sa.Column("source_system", sa.String(10), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("summary", sa.Text),
        sa.Column("raw_data", sa.dialects.postgresql.JSONB, server_default="{}"),
        sa.Column("content_hash", sa.String(80), nullable=False),
        sa.Column("confidence", sa.Integer, server_default="3"),
        sa.Column("is_signed", sa.Boolean, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("confidence BETWEEN 1 AND 4", name="evidence_confidence_chk"),
    )
    op.create_index("ix_evidence_source_system", "evidence", ["source_system"])
    op.create_index("ix_evidence_case_id", "evidence", ["case_id"])
    op.create_index("ix_evidence_source_case", "evidence", ["source_system", "case_id"])

    # 时间线事件表
    op.create_table(
        "timeline_events",
        sa.Column("id", sa.String(50), primary_key=True),
        sa.Column("case_id", sa.String(32), sa.ForeignKey("deviations.id"), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("detail", sa.Text),
        sa.Column("severity", sa.String(20), server_default="info"),
        sa.Column("evidence_id", sa.String(50), sa.ForeignKey("evidence.id")),
        sa.Column("is_anomaly", sa.Boolean, server_default=sa.false()),
        sa.Column("sort_order", sa.Integer, server_default="0"),
    )
    op.create_index("ix_timeline_case_id", "timeline_events", ["case_id"])

    # 5 Whys 表
    op.create_table(
        "five_whys",
        sa.Column("id", sa.String(50), primary_key=True),
        sa.Column("case_id", sa.String(32), sa.ForeignKey("deviations.id"), nullable=False),
        sa.Column("n", sa.Integer, nullable=False),
        sa.Column("question", sa.Text),
        sa.Column("answer", sa.Text),
        sa.Column("status", sa.String(20), server_default="pending"),
        sa.Column("evidence_ids", sa.dialects.postgresql.ARRAY(sa.String)),
        sa.Column("is_root_cause", sa.Boolean, server_default=sa.false()),
        sa.Column("ai_model", sa.String(100)),
        sa.Column("ai_output_hash", sa.String(80)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_five_whys_case_id", "five_whys", ["case_id"])

    # Skeptic 挑战表
    op.create_table(
        "skeptic_challenges",
        sa.Column("id", sa.String(50), primary_key=True),
        sa.Column("case_id", sa.String(32), sa.ForeignKey("deviations.id"), nullable=False),
        sa.Column("target", sa.Text, nullable=False),
        sa.Column("challenge", sa.Text, nullable=False),
        sa.Column("evidence_ids", sa.dialects.postgresql.ARRAY(sa.String)),
        sa.Column("status", sa.String(20), server_default="pending"),
        sa.Column("resolution", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_skeptic_case_id", "skeptic_challenges", ["case_id"])

    # 审计日志表 —— GxP 不可篡改
    op.create_table(
        "audit_log",
        sa.Column("log_id", sa.String(80), primary_key=True),
        sa.Column("seq", sa.Integer, nullable=False, unique=True),
        sa.Column("operator", sa.String(100), nullable=False),
        sa.Column("action", sa.String(50), nullable=False),
        sa.Column("target", sa.String(200)),
        sa.Column("detail", sa.Text),
        sa.Column("ip_address", sa.String(50)),
        sa.Column("session_id", sa.String(80)),
        sa.Column("evidence_refs", sa.dialects.postgresql.ARRAY(sa.String)),
        sa.Column("ai_model", sa.String(100)),
        sa.Column("ai_prompt_hash", sa.String(80)),
        sa.Column("ai_output_hash", sa.String(80)),
        sa.Column("prev_hash", sa.String(80), nullable=False),
        sa.Column("current_hash", sa.String(80), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_audit_log_action", "audit_log", ["action"])
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])

    # 审计日志不可篡改触发器
    op.execute("""
        CREATE OR REPLACE FUNCTION audit_log_immutable_guard() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log 表受 GxP 合规保护，禁止 % 操作 (log_id=%, seq=%)',
                TG_OP, COALESCE(OLD.log_id, NEW.log_id), COALESCE(OLD.seq, NEW.seq);
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_audit_log_no_update BEFORE UPDATE ON audit_log
            FOR EACH ROW EXECUTE FUNCTION audit_log_immutable_guard();

        CREATE TRIGGER trg_audit_log_no_delete BEFORE DELETE ON audit_log
            FOR EACH ROW EXECUTE FUNCTION audit_log_immutable_guard();
    """)

    # Agent 运行记录表
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.String(50), primary_key=True),
        sa.Column("case_id", sa.String(32), sa.ForeignKey("deviations.id"), nullable=False),
        sa.Column("status", sa.String(20), server_default="pending"),
        sa.Column("current_agent", sa.String(30)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("agent_states", sa.dialects.postgresql.JSONB, server_default="{}"),
        sa.Column("triggered_by", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_agent_runs_case_id", "agent_runs", ["case_id"])

    # 源系统注册表
    op.create_table(
        "source_systems",
        sa.Column("code", sa.String(10), primary_key=True),
        sa.Column("label", sa.String(100), nullable=False),
        sa.Column("icon", sa.String(50), server_default="database"),
        sa.Column("color", sa.String(30), server_default="src-lims"),
        sa.Column("description", sa.Text),
        sa.Column("connector_url", sa.String(200)),
        sa.Column("health_status", sa.String(20), server_default="unknown"),
        sa.Column("last_sync_at", sa.DateTime(timezone=True)),
        sa.Column("is_readonly", sa.Boolean, nullable=False, server_default=sa.true()),
    )

    # 电子签名记录表 —— 21 CFR Part 11
    op.create_table(
        "signatures",
        sa.Column("id", sa.String(50), primary_key=True),
        sa.Column("case_id", sa.String(32), sa.ForeignKey("deviations.id"), nullable=False),
        sa.Column("signer_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("signer_role", sa.String(30), nullable=False),
        sa.Column("signature_meaning", sa.String(100), nullable=False),
        sa.Column("signed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("certificate_fingerprint", sa.String(80)),
        sa.Column("ip_address", sa.String(50)),
        sa.Column("confirmation_text", sa.Text, server_default="我确认本电子签名等同于手写签名，所提交内容真实准确"),
    )
    op.create_index("ix_signatures_case_id", "signatures", ["case_id"])


def downgrade() -> None:
    """回滚初始 schema"""
    op.drop_table("signatures")
    op.drop_table("source_systems")
    op.drop_table("agent_runs")
    op.execute("DROP TRIGGER IF EXISTS trg_audit_log_no_delete ON audit_log")
    op.execute("DROP TRIGGER IF EXISTS trg_audit_log_no_update ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS audit_log_immutable_guard()")
    op.drop_table("audit_log")
    op.drop_table("skeptic_challenges")
    op.drop_table("five_whys")
    op.drop_table("timeline_events")
    op.drop_table("evidence")
    op.drop_table("deviations")
    op.drop_table("users")
