"""部署迁移入口 —— docker compose `migrate` 一次性服务调用。

兼容三类数据库状态：
- 空库（无 users 表）: 直接 `alembic upgrade head`，由 0001 全量建表；
- 由 db/01_schema.sql 引导的存量库（有 users 表但无 alembic_version）:
  先 `alembic stamp head` 再 upgrade —— schema.sql 与迁移头保持同步是仓库既有约定
  （CI alembic check 把关漂移），stamp 让后续增量迁移可以正常接管；
- 已由 alembic 版本化的库: 常规 `alembic upgrade head`。

用法（容器内）: cwd=/app/backend && python scripts/run_migrations.py
"""
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

import psycopg2  # noqa: E402

from shared.config import settings  # noqa: E402


def _table_exists(conn, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = %s",
            (table,),
        )
        return cur.fetchone() is not None


def _alembic(*args: str) -> None:
    # 用 alembic 控制台脚本而非 `python -m alembic`：后者会把 CWD 加进 sys.path，
    # 在 backend/ 下与本地 `alembic/` 迁移包同名冲突（circular import）
    subprocess.check_call(["alembic", *args], cwd=BACKEND_DIR)


def main() -> None:
    # env.py 同样使用 psycopg2 同步驱动（多语句 DDL 兼容）；
    # psycopg2 的 DSN 不带方言后缀（postgresql+psycopg2:// 是 SQLAlchemy 语法）
    sync_dsn = settings.postgres_dsn.replace("postgresql+asyncpg://", "postgresql://")
    with psycopg2.connect(sync_dsn) as conn:
        has_users = _table_exists(conn, "users")
        has_version = _table_exists(conn, "alembic_version")

    if has_users and not has_version:
        print(
            "[migrate] legacy schema without alembic_version detected — stamping head. "
            "Ensure db/01_schema.sql stays in sync with migration head (CI alembic check).",
            flush=True,
        )
        _alembic("stamp", "head")
        # 兜底调和：老版本 01_schema.sql 引导的库 stamp head 后，0005 的索引迁移体
        # 不会执行（stamp 只记账）—— 幂等补建部分唯一索引，保证 schema.sql↔head 契约成立
        with psycopg2.connect(sync_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_agentruns_one_active_per_case "
                    "ON agent_runs (case_id) WHERE status IN ('pending', 'running')"
                )
                # 0009 调和：老 01_schema.sql 引导的库 signatures 无 revision 列、
                # ux_ 唯一索引为三列 —— stamp 不执行迁移体，幂等补列并扩为四列
                cur.execute(
                    "ALTER TABLE signatures ADD COLUMN IF NOT EXISTS revision "
                    "INTEGER NOT NULL DEFAULT 0"
                )
                cur.execute("DROP INDEX IF EXISTS ux_signatures_case_meaning_signer")
                cur.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_signatures_case_meaning_signer "
                    "ON signatures (case_id, signature_meaning, signer_id, revision)"
                )
        print("[migrate] reconciliation: partial unique index + signatures.revision ensured", flush=True)

    _alembic("upgrade", "head")
    print("[migrate] alembic upgrade head completed", flush=True)


if __name__ == "__main__":
    main()
