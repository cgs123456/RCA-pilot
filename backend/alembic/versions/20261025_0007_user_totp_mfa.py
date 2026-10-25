"""0007: users 表新增 TOTP 第二因子字段（P3-④）

背景：
    21 CFR Part 11 要求登录具备可归责的身份校验；审查报告 P3-④ 要求实现
    TOTP/MFA。此前 auth_service 登录流程完全不消费 mfa_code（config.py:51
    注释承认「代码尚未实现 TOTP 校验」）。

变更（users 表，全部向后兼容、存量行无需回填）：
    - totp_secret   VARCHAR(64)  NULL   —— base32 种子；NULL=未启用
    - totp_pending  BOOLEAN      NOT NULL DEFAULT 0 —— setup 后尚未验证激活
    - totp_last_step INTEGER     NOT NULL DEFAULT 0 —— 最近命中的时间片，防同窗口重放

登录语义（auth_service/routes.py）：
    totp_secret 非空且 totp_pending=False 时，登录必须携带有效 mfa_code。
    激活流程：POST /auth/totp/setup（生成种子，pending=True）
           → 用户扫码 → POST /auth/totp/activate（验证一次 code，pending=False）。
"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("totp_secret", sa.String(64), nullable=True))
    op.add_column(
        "users",
        sa.Column("totp_pending", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "users",
        sa.Column("totp_last_step", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )


def downgrade() -> None:
    op.drop_column("users", "totp_last_step")
    op.drop_column("users", "totp_pending")
    op.drop_column("users", "totp_secret")
