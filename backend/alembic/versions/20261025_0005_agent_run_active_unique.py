"""agent_runs 增加部分唯一索引 —— DB 级兜底「同案例只允许一条活跃流水线」。

历史问题：重复触发防护仅靠应用层查重（agent_service/routes.py 中先 SELECT
ACTIVE_RUN_STATUSES 再 INSERT），两个并发请求都通过查重后可同时插入 pending run，
同案例出现两条活跃流水线（审计/时间线/HITL 语义全部失真）。

本迁移在 DB 层补上最后防线：
- 唯一索引只约束 status IN ('pending','running') 的行（与 agent_service 的
  ACTIVE_RUN_STATUSES 常量对齐，paused/completed/failed/cancelled 的历史行不受限）；
- SQLite（测试环境）用 sqlite_where 同语义建部分索引，create_all 与迁移保持一致。

回滚：直接 DROP INDEX（无数据变更，可安全回退）。
"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels = None
depends_on = None

_INDEX_NAME = "uq_agentruns_one_active_per_case"
_PARTIAL_WHERE = sa.text("status IN ('pending', 'running')")


def upgrade() -> None:
    # 幂等保护：存量环境可能已手工建过同名索引
    conn = op.get_bind()
    existing = conn.execute(
        sa.text("SELECT 1 FROM pg_indexes WHERE indexname = :name"),
        {"name": _INDEX_NAME},
    ).scalar()
    if existing:
        return
    op.create_index(
        _INDEX_NAME,
        "agent_runs",
        ["case_id"],
        unique=True,
        postgresql_where=_PARTIAL_WHERE,
    )


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name="agent_runs")
