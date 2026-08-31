"""Alembic 迁移包 —— 通用工具函数"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op


def create_updated_at_trigger(table_name: str) -> None:
    """创建 updated_at 自动更新触发器"""
    op.execute(f"""
        CREATE OR REPLACE FUNCTION update_updated_at_{table_name}()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_{table_name}_updated_at
            BEFORE UPDATE ON {table_name}
            FOR EACH ROW
            EXECUTE FUNCTION update_updated_at_{table_name}();
    """)


def drop_updated_at_trigger(table_name: str) -> None:
    """删除 updated_at 自动更新触发器"""
    op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_updated_at ON {table_name}")
    op.execute(f"DROP FUNCTION IF EXISTS update_updated_at_{table_name}()")
