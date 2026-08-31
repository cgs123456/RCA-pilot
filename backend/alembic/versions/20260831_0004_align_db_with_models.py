"""对齐迁移库与 ORM（第 3 批）：NOT NULL 收紧 + 移除冗余 server 默认 + 索引重命名

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-31

历史问题：0001 从旧版 01_schema.sql 移植时丢失了大量 NOT NULL（仅保留 server
默认），而 ORM 用 Mapped[T]（非 Optional）推断这些列应为 NOT NULL 且只在应用层
用 Python 默认值（default=）填充、不声明 server_default。同时模型的索引命名与
0001 建的库不一致，且 users.email 在库里同时存在列级唯一约束（users_email_key）
与唯一索引（ix_users_email）冗余两份。

本迁移把"迁移库建出的 DB"对齐到"ORM 模型现状"：
  1. 对这些列 SET NOT NULL（DB 现为 nullable，模型要求 NOT NULL）；
  2. 对模型未声明 server_default 的列 DROP DEFAULT（避免与 ORM 声明漂移）；
  3. 重命名索引 ix_skeptic_case_id→ix_skeptic_challenges_case_id、
     ix_timeline_case_id→ix_timeline_events_case_id（对齐模型 index= 派生的名字）；
  4. 移除 users.email 的冗余列级唯一约束（保留唯一索引 ix_users_email）。

说明：审计/签名表上的防篡改触发器创建在 0001/0002，本迁移不涉及。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _drop_default_and_not_null(table: str, column: str, existing_type: object) -> None:
    """DROP DEFAULT + SET NOT NULL（DB 现为 nullable，模型要求 NOT NULL 且无 server 默认）"""
    op.alter_column(
        table,
        column,
        existing_type=existing_type,
        server_default=None,
    )
    op.alter_column(
        table,
        column,
        existing_type=existing_type,
        existing_nullable=True,
        nullable=False,
    )


def _drop_default(table: str, column: str, existing_type: object) -> None:
    """仅 DROP DEFAULT（列已经是 NOT NULL，无需改 nullable）"""
    op.alter_column(
        table,
        column,
        existing_type=existing_type,
        server_default=None,
    )


def upgrade() -> None:
    # ---- agent_runs：status / agent_states / created_at ----
    _drop_default_and_not_null("agent_runs", "status", sa.String(20))
    _drop_default_and_not_null("agent_runs", "agent_states", sa.dialects.postgresql.JSONB)
    _drop_default_and_not_null("agent_runs", "created_at", sa.DateTime(timezone=True))

    # ---- audit_log ----
    op.alter_column(
        "audit_log",
        "evidence_refs",
        existing_type=sa.dialects.postgresql.ARRAY(sa.String()),
        existing_nullable=True,
        nullable=False,
    )
    _drop_default("audit_log", "created_at", sa.DateTime(timezone=True))

    # ---- capas：仅移除 server 默认 ----
    _drop_default("capas", "status", sa.String(30))
    _drop_default("capas", "created_at", sa.DateTime(timezone=True))
    _drop_default("capas", "updated_at", sa.DateTime(timezone=True))

    # ---- deviations ----
    _drop_default_and_not_null("deviations", "complexity", sa.Integer())
    _drop_default_and_not_null("deviations", "progress", sa.Integer())
    _drop_default_and_not_null("deviations", "hitl_gates", sa.dialects.postgresql.JSONB)
    _drop_default_and_not_null("deviations", "meta", sa.dialects.postgresql.JSONB)
    _drop_default_and_not_null("deviations", "created_at", sa.DateTime(timezone=True))
    _drop_default_and_not_null("deviations", "updated_at", sa.DateTime(timezone=True))

    # ---- evidence ----
    _drop_default_and_not_null("evidence", "raw_data", sa.dialects.postgresql.JSONB)
    _drop_default_and_not_null("evidence", "confidence", sa.Integer())
    _drop_default_and_not_null("evidence", "is_signed", sa.Boolean())
    _drop_default_and_not_null("evidence", "created_at", sa.DateTime(timezone=True))

    # ---- five_whys ----
    _drop_default_and_not_null("five_whys", "status", sa.String(20))
    op.alter_column(
        "five_whys",
        "evidence_ids",
        existing_type=sa.dialects.postgresql.ARRAY(sa.String()),
        existing_nullable=True,
        nullable=False,
    )
    _drop_default_and_not_null("five_whys", "is_root_cause", sa.Boolean())
    _drop_default_and_not_null("five_whys", "created_at", sa.DateTime(timezone=True))
    _drop_default_and_not_null("five_whys", "updated_at", sa.DateTime(timezone=True))

    # ---- reports：仅移除 server 默认 ----
    _drop_default("reports", "version", sa.Integer())
    _drop_default("reports", "status", sa.String(30))
    _drop_default("reports", "created_at", sa.DateTime(timezone=True))
    _drop_default("reports", "updated_at", sa.DateTime(timezone=True))

    # ---- signatures ----
    _drop_default("signatures", "signed_at", sa.DateTime(timezone=True))
    _drop_default_and_not_null("signatures", "confirmation_text", sa.Text())

    # ---- skeptic_challenges ----
    op.alter_column(
        "skeptic_challenges",
        "evidence_ids",
        existing_type=sa.dialects.postgresql.ARRAY(sa.String()),
        existing_nullable=True,
        nullable=False,
    )
    _drop_default_and_not_null("skeptic_challenges", "status", sa.String(20))
    _drop_default_and_not_null("skeptic_challenges", "created_at", sa.DateTime(timezone=True))
    op.drop_index("ix_skeptic_case_id", table_name="skeptic_challenges")
    op.create_index("ix_skeptic_challenges_case_id", "skeptic_challenges", ["case_id"])

    # ---- source_systems ----
    _drop_default_and_not_null("source_systems", "icon", sa.String(50))
    _drop_default_and_not_null("source_systems", "color", sa.String(30))
    _drop_default_and_not_null("source_systems", "health_status", sa.String(20))
    _drop_default("source_systems", "is_readonly", sa.Boolean())

    # ---- timeline_events ----
    _drop_default_and_not_null("timeline_events", "severity", sa.String(20))
    _drop_default_and_not_null("timeline_events", "is_anomaly", sa.Boolean())
    _drop_default_and_not_null("timeline_events", "sort_order", sa.Integer())
    op.drop_index("ix_timeline_case_id", table_name="timeline_events")
    op.create_index("ix_timeline_events_case_id", "timeline_events", ["case_id"])

    # ---- users ----
    _drop_default("users", "color", sa.String(20))
    _drop_default("users", "is_active", sa.Boolean())
    _drop_default_and_not_null("users", "failed_login_attempts", sa.Integer())
    _drop_default_and_not_null("users", "created_at", sa.DateTime(timezone=True))
    _drop_default_and_not_null("users", "updated_at", sa.DateTime(timezone=True))
    # 移除 0001 里 email 列级唯一约束（users_email_key）—— 唯一索引 ix_users_email 保留即够
    op.drop_constraint("users_email_key", "users", type_="unique")


def downgrade() -> None:
    # ---- users ----
    op.create_unique_constraint("users_email_key", "users", ["email"])
    _restore_default_and_nullable("users", "created_at", sa.DateTime(timezone=True), "now()")
    _restore_default_and_nullable("users", "updated_at", sa.DateTime(timezone=True), "now()")
    _restore_default_and_nullable("users", "failed_login_attempts", sa.Integer(), "0")
    _restore_default("users", "is_active", sa.Boolean(), "true")
    _restore_default("users", "color", sa.String(20), "'ink'")

    # ---- timeline_events ----
    op.drop_index("ix_timeline_events_case_id", table_name="timeline_events")
    op.create_index("ix_timeline_case_id", "timeline_events", ["case_id"])
    _restore_default_and_nullable("timeline_events", "sort_order", sa.Integer(), "0")
    _restore_default_and_nullable("timeline_events", "is_anomaly", sa.Boolean(), "false")
    _restore_default_and_nullable("timeline_events", "severity", sa.String(20), "'info'")

    # ---- source_systems ----
    _restore_default("source_systems", "is_readonly", sa.Boolean(), "true")
    _restore_default_and_nullable("source_systems", "health_status", sa.String(20), "'unknown'")
    _restore_default_and_nullable("source_systems", "color", sa.String(30), "'src-lims'")
    _restore_default_and_nullable("source_systems", "icon", sa.String(50), "'database'")

    # ---- skeptic_challenges ----
    op.drop_index("ix_skeptic_challenges_case_id", table_name="skeptic_challenges")
    op.create_index("ix_skeptic_case_id", "skeptic_challenges", ["case_id"])
    _restore_default_and_nullable("skeptic_challenges", "created_at", sa.DateTime(timezone=True), "now()")
    _restore_default_and_nullable("skeptic_challenges", "status", sa.String(20), "'pending'")
    _restore_nullable("skeptic_challenges", "evidence_ids", sa.dialects.postgresql.ARRAY(sa.String()))

    # ---- signatures ----
    _restore_default_and_nullable("signatures", "confirmation_text", sa.Text(), "'我确认本电子签名等同于手写签名，所提交内容真实准确'")
    _restore_default("signatures", "signed_at", sa.DateTime(timezone=True), "now()")

    # ---- reports ----
    _restore_default("reports", "updated_at", sa.DateTime(timezone=True), "now()")
    _restore_default("reports", "created_at", sa.DateTime(timezone=True), "now()")
    _restore_default("reports", "status", sa.String(30), "'draft'")
    _restore_default("reports", "version", sa.Integer(), "1")

    # ---- five_whys ----
    _restore_nullable("five_whys", "evidence_ids", sa.dialects.postgresql.ARRAY(sa.String()))
    _restore_default_and_nullable("five_whys", "updated_at", sa.DateTime(timezone=True), "now()")
    _restore_default_and_nullable("five_whys", "created_at", sa.DateTime(timezone=True), "now()")
    _restore_default_and_nullable("five_whys", "is_root_cause", sa.Boolean(), "false")
    _restore_default_and_nullable("five_whys", "status", sa.String(20), "'pending'")

    # ---- evidence ----
    _restore_default_and_nullable("evidence", "created_at", sa.DateTime(timezone=True), "now()")
    _restore_default_and_nullable("evidence", "is_signed", sa.Boolean(), "false")
    _restore_default_and_nullable("evidence", "confidence", sa.Integer(), "3")
    _restore_default_and_nullable("evidence", "raw_data", sa.dialects.postgresql.JSONB, "'{}'")

    # ---- deviations ----
    _restore_default_and_nullable("deviations", "updated_at", sa.DateTime(timezone=True), "now()")
    _restore_default_and_nullable("deviations", "created_at", sa.DateTime(timezone=True), "now()")
    _restore_default_and_nullable("deviations", "meta", sa.dialects.postgresql.JSONB, "'{}'")
    _restore_default_and_nullable("deviations", "hitl_gates", sa.dialects.postgresql.JSONB, "'{}'")
    _restore_default_and_nullable("deviations", "progress", sa.Integer(), "0")
    _restore_default_and_nullable("deviations", "complexity", sa.Integer(), "1")

    # ---- capas ----
    _restore_default("capas", "updated_at", sa.DateTime(timezone=True), "now()")
    _restore_default("capas", "created_at", sa.DateTime(timezone=True), "now()")
    _restore_default("capas", "status", sa.String(30), "'pending'")

    # ---- audit_log ----
    _restore_default("audit_log", "created_at", sa.DateTime(timezone=True), "now()")
    _restore_nullable("audit_log", "evidence_refs", sa.dialects.postgresql.ARRAY(sa.String()))

    # ---- agent_runs ----
    _restore_default_and_nullable("agent_runs", "created_at", sa.DateTime(timezone=True), "now()")
    _restore_default_and_nullable("agent_runs", "agent_states", sa.dialects.postgresql.JSONB, "'{}'")
    _restore_default_and_nullable("agent_runs", "status", sa.String(20), "'pending'")


def _set_default(table: str, column: str, existing_type: object, default: str) -> None:
    op.alter_column(
        table,
        column,
        existing_type=existing_type,
        server_default=sa.text(default),
    )


def _set_nullable(table: str, column: str, existing_type: object, nullable: bool) -> None:
    op.alter_column(
        table,
        column,
        existing_type=existing_type,
        existing_nullable=not nullable,
        nullable=nullable,
    )


def _restore_default(table: str, column: str, existing_type: object, default: str) -> None:
    """旧列原本就有默认值（无需改 nullable），重新加回 server 默认"""
    _set_default(table, column, existing_type, default)


def _restore_nullable(table: str, column: str, existing_type: object) -> None:
    """旧列原本 nullable=True（无默认值），重新放开 NULL"""
    _set_nullable(table, column, existing_type, True)


def _restore_default_and_nullable(table: str, column: str, existing_type: object, default: str) -> None:
    """旧列原本 nullable=True 且有 server 默认，恢复两者"""
    _set_default(table, column, existing_type, default)
    _set_nullable(table, column, existing_type, True)
