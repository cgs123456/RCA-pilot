"""对齐迁移库与 ORM：embedding 三列、审计链字段、签名/5Whys 约束、缺索引

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-30

历史问题：ORM（shared/models.py）与 0001 迁移漂移 —— 语义检索的 embedding
三列在迁移库中缺失（导致 alembic upgrade 后语义检索直接 UndefinedColumn），
且多处唯一约束/索引/字段缺失。本迁移将迁移库对齐到 ORM 现状。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- users: 锁定时间窗 ----
    op.add_column("users", sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True))

    # ---- deviations: 结案时间（KPI 实算）+ 发现时间索引 ----
    op.add_column("deviations", sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_deviations_discovered_at", "deviations", ["discovered_at"])

    # ---- evidence: 语义检索三列（0001 缺失 —— 历史上 alembic 建的库上语义检索直接崩溃）----
    op.add_column("evidence", sa.Column("embedding", postgresql.JSONB, nullable=True))
    op.add_column("evidence", sa.Column("embedding_model", sa.String(80), nullable=True))
    op.add_column("evidence", sa.Column("embedded_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_evidence_collected_at", "evidence", ["collected_at"])

    # ---- timeline_events ----
    op.create_index("ix_timeline_occurred_at", "timeline_events", ["occurred_at"])

    # ---- five_whys: (case_id, n) 唯一 ----
    op.create_unique_constraint("five_whys_case_n_uniq", "five_whys", ["case_id", "n"])

    # ---- audit_log: seq 升级 BigInteger + trace_id + 操作员索引 + 防篡改触发器补全 ----
    op.alter_column("audit_log", "seq", type_=sa.BigInteger(), existing_type=sa.Integer(), existing_nullable=False)
    op.add_column("audit_log", sa.Column("trace_id", sa.String(36), nullable=True))
    op.create_index("ix_audit_operator_created", "audit_log", ["operator", "created_at"])

    # TRUNCATE 不触发 DELETE 触发器 —— 补上这一旁路防护
    op.execute("""
        CREATE OR REPLACE FUNCTION audit_log_no_truncate_guard() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log 表受 GxP 合规保护，禁止 TRUNCATE 操作';
        END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS trg_audit_log_no_truncate ON audit_log;
        CREATE TRIGGER trg_audit_log_no_truncate BEFORE TRUNCATE ON audit_log
            FOR STATEMENT EXECUTE FUNCTION audit_log_no_truncate_guard();
    """)

    # ---- agent_runs: 失败原因 + 状态索引 ----
    op.add_column("agent_runs", sa.Column("error_message", sa.Text, nullable=True))
    op.create_index("ix_agentruns_status", "agent_runs", ["status"])

    # ---- signatures: 双签唯一约束 + Part 11 防篡改触发器 ----
    op.create_unique_constraint(
        "signatures_case_meaning_signer_uniq", "signatures", ["case_id", "signature_meaning", "signer_id"]
    )
    op.execute("""
        CREATE OR REPLACE FUNCTION signatures_immutable_guard() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'signatures 表受 21 CFR Part 11 保护，禁止 % 操作', TG_OP;
        END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS trg_signatures_no_update ON signatures;
        CREATE TRIGGER trg_signatures_no_update BEFORE UPDATE ON signatures
            FOR EACH ROW EXECUTE FUNCTION signatures_immutable_guard();

        DROP TRIGGER IF EXISTS trg_signatures_no_delete ON signatures;
        CREATE TRIGGER trg_signatures_no_delete BEFORE DELETE ON signatures
            FOR EACH ROW EXECUTE FUNCTION signatures_immutable_guard();

        DROP TRIGGER IF EXISTS trg_signatures_no_truncate ON signatures;
        CREATE TRIGGER trg_signatures_no_truncate BEFORE TRUNCATE ON signatures
            FOR STATEMENT EXECUTE FUNCTION signatures_immutable_guard();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_signatures_no_truncate ON signatures")
    op.execute("DROP TRIGGER IF EXISTS trg_signatures_no_delete ON signatures")
    op.execute("DROP TRIGGER IF EXISTS trg_signatures_no_update ON signatures")
    op.execute("DROP FUNCTION IF EXISTS signatures_immutable_guard()")
    op.drop_constraint("signatures_case_meaning_signer_uniq", "signatures", type_="unique")

    op.drop_index("ix_agentruns_status", table_name="agent_runs")
    op.drop_column("agent_runs", "error_message")

    op.execute("DROP TRIGGER IF EXISTS trg_audit_log_no_truncate ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS audit_log_no_truncate_guard()")
    op.drop_index("ix_audit_operator_created", table_name="audit_log")
    op.drop_column("audit_log", "trace_id")
    op.alter_column("audit_log", "seq", type_=sa.Integer(), existing_type=sa.BigInteger(), existing_nullable=False)

    op.drop_constraint("five_whys_case_n_uniq", "five_whys", type_="unique")
    op.drop_index("ix_timeline_occurred_at", table_name="timeline_events")
    op.drop_index("ix_evidence_collected_at", table_name="evidence")
    op.drop_column("evidence", "embedded_at")
    op.drop_column("evidence", "embedding_model")
    op.drop_column("evidence", "embedding")
    op.drop_index("ix_deviations_discovered_at", table_name="deviations")
    op.drop_column("deviations", "closed_at")
    op.drop_column("users", "locked_until")
