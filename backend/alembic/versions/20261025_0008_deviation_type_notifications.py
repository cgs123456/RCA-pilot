"""0008: 偏差类型/批次影响字段 + 站内通知表（P2-⑧ 业务补全最小闭环）

背景：
    审查报告 3.2-4 指出「偏差分级 / OOS-OOT 联动 / 批次影响评估 / 协作通知」
    均无实现。本迁移补齐数据基础：
    - deviations.deviation_type   VARCHAR(32) NOT NULL DEFAULT 'process_deviation'
      取值：process_deviation|lab_oos|lab_oot|complaint|equipment|environment|other
      （OOS=检验结果超标，OOT=趋势外 —— 制药偏差分级的基础维度）
    - deviations.affected_batches JSONB NOT NULL DEFAULT '[]'
      受影响批号列表，按批号可追溯产品影响
    - notifications 新表：站内协作通知（指派/结案/CAPA/关卡就绪）

全部向后兼容：存量偏差行获得默认类型与空批次列表。
"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "deviations",
        sa.Column(
            "deviation_type",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'process_deviation'"),
        ),
    )
    op.add_column(
        "deviations",
        sa.Column(
            "affected_batches",
            JSONB(),
            nullable=False,
            # 不带 ::jsonb cast：字面量由 PG 按列类型自动转换；
            # 与 models server_default 保持逐字一致（alembic check 对比渲染文本）
            server_default=sa.text("'[]'"),
        ),
    )
    op.create_index("ix_deviations_deviation_type", "deviations", ["deviation_type"])

    op.create_table(
        "notifications",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column(
            "user_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column(
            "case_id", sa.String(32), sa.ForeignKey("deviations.id"), nullable=True
        ),
        sa.Column("type", sa.String(50), nullable=False),
        # 新建空表无需 server_default：ORM 始终显式提供 payload/created_at
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_notifications_user_created", "notifications", ["user_id", "created_at"]
    )
    op.create_index(
        "ix_notifications_unread",
        "notifications",
        ["user_id"],
        postgresql_where=sa.text("read_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_notifications_unread", table_name="notifications")
    op.drop_index("ix_notifications_user_created", table_name="notifications")
    op.drop_table("notifications")
    op.drop_index("ix_deviations_deviation_type", table_name="deviations")
    op.drop_column("deviations", "affected_batches")
    op.drop_column("deviations", "deviation_type")
