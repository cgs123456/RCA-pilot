"""对齐迁移库与 ORM（第 2 批）：CAPA / ICH Q9 Report 表 + deviations 风险矩阵字段

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-30

背景：Phase 2 / Roadmap 新增了 Capa（纠正预防措施）与 Report（ICH Q9 十节格式报告）
两张 ORM 模型，并在 Deviation 上扩展了风险矩阵三字段（risk_rpn / risk_category /
risk_owner），但此前 0001/0002 迁移与 01_schema.sql 均未创建，导致：
- alembic check 对照 Base.metadata 报缺表/缺列；
- ORM 在访问 Capa/Report 相关关系时触发 mapper 配置错误或 UndefinedColumn。
本迁移把这两张表和 risk 三列补进迁移库。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- deviations: ICH Q9 风险矩阵三字段 ----
    op.add_column("deviations", sa.Column("risk_rpn", sa.Integer(), nullable=True))
    op.add_column("deviations", sa.Column("risk_category", sa.String(20), nullable=True))
    op.add_column("deviations", sa.Column("risk_owner", sa.String(32), nullable=True))
    op.create_foreign_key("fk_deviations_risk_owner_users", "deviations", "users", ["risk_owner"], ["id"])

    # ---- capas ----
    op.create_table(
        "capas",
        sa.Column("id", sa.String(50), primary_key=True),
        sa.Column("case_id", sa.String(32), sa.ForeignKey("deviations.id"), nullable=False, index=True),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("responsibility", sa.String(100), nullable=False),
        sa.Column("due_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_by", sa.String(32), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("effectiveness_rate", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_capas_status", "capas", ["status"])
    op.create_index("ix_capas_due_date", "capas", ["due_date"])

    # ---- reports（ICH Q9 十节格式，版本化管理）----
    op.create_table(
        "reports",
        sa.Column("id", sa.String(50), primary_key=True),
        sa.Column("case_id", sa.String(32), sa.ForeignKey("deviations.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="draft"),
        sa.Column("author_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("approved_by", sa.String(32), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("change_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_unique_constraint("reports_case_version_uniq", "reports", ["case_id", "version"])
    op.create_index("ix_reports_case_id", "reports", ["case_id"])
    op.create_index("ix_reports_status", "reports", ["status"])


def downgrade() -> None:
    op.drop_index("ix_reports_status", table_name="reports")
    op.drop_index("ix_reports_case_id", table_name="reports")
    op.drop_constraint("reports_case_version_uniq", "reports", type_="unique")
    op.drop_table("reports")

    op.drop_index("ix_capas_due_date", table_name="capas")
    op.drop_index("ix_capas_status", table_name="capas")
    op.drop_table("capas")

    op.drop_constraint("fk_deviations_risk_owner_users", "deviations", type_="foreignkey")
    op.drop_column("deviations", "risk_owner")
    op.drop_column("deviations", "risk_category")
    op.drop_column("deviations", "risk_rpn")