"""signatures 增加 revision 修订轮次 —— 结案重审（reopen）后的受控重签。

历史问题：signatures_case_meaning_signer_uniq 三列唯一（case/meaning/signer）使
reopen 后同一签字人永远无法重新提交/批准（重签死锁）；models.py 已将唯一约束
扩为四列（含 revision），但迁移缺失导致 alembic check 漂移。

本迁移：
- 补 revision 列（INTEGER NOT NULL DEFAULT 0，0=首次签署）；
- 唯一约束扩为四列（同一签字人在不同修订轮次可再次签署，
  旧签名记录不可篡改地保留，21 CFR Part 11）；
- 幂等防御：兼容由 01_schema.sql 引导、stamp 落在链中的存量库
  （可能只有 ux_ 唯一索引而无同名约束）。

回滚：恢复三列约束后删除 revision 列（历史行 revision 均为 0，三列语义等价）。
降级前提：若库中已存在 revision>0 的重签记录（reopen 后），同一 (case, meaning,
signer) 会出现多行，三列约束重建将因唯一冲突失败 —— 降级前需先人工归档/合并
重签历史（GxP 签名记录不可自动删改，故不在迁移内做数据合并）。
"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels = None
depends_on = None

_UQ_NAME = "signatures_case_meaning_signer_uniq"
_LEGACY_UX_NAME = "ux_signatures_case_meaning_signer"


def _constraint_exists(conn, name: str) -> bool:
    return conn.execute(
        sa.text(
            "SELECT 1 FROM pg_constraint "
            "WHERE conname = :name AND conrelid = 'signatures'::regclass"
        ),
        {"name": name},
    ).scalar() is not None


def _column_exists(conn, name: str) -> bool:
    return conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'signatures' AND column_name = :name"
        ),
        {"name": name},
    ).scalar() is not None


def upgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn, "revision"):
        op.add_column(
            "signatures",
            sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("0")),
        )
    if _constraint_exists(conn, _UQ_NAME):
        op.drop_constraint(_UQ_NAME, "signatures", type_="unique")
    op.create_unique_constraint(
        _UQ_NAME,
        "signatures",
        ["case_id", "signature_meaning", "signer_id", "revision"],
    )
    # 由 01_schema.sql 引导的库用同名唯一索引做签名查重；三列旧索引会继续
    # 阻止跨修订轮次重签，存在则替换（与新约束并存也属冗余）
    legacy_ux = conn.execute(
        sa.text("SELECT 1 FROM pg_indexes WHERE indexname = :name"),
        {"name": _LEGACY_UX_NAME},
    ).scalar()
    if legacy_ux:
        op.drop_index(_LEGACY_UX_NAME, table_name="signatures")


def downgrade() -> None:
    conn = op.get_bind()
    if _constraint_exists(conn, _UQ_NAME):
        op.drop_constraint(_UQ_NAME, "signatures", type_="unique")
    op.create_unique_constraint(
        _UQ_NAME,
        "signatures",
        ["case_id", "signature_meaning", "signer_id"],
    )
    op.drop_column("signatures", "revision")
