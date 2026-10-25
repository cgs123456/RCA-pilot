"""0006: pgvector 启用与 embedding 向量列回填

背景（H20/C13）：
- 旧实现把 embedding 存 JSONB、检索在 Python 里逐行余弦，候选上限 2000 条，
  老证据静默丢失，且 2000×1024 维纯 Python 余弦单次搜索阻塞事件循环 0.6-1s。
- 本迁移启用 pgvector 扩展并新增 `evidence.embedding_vec` 列（JSONB 仍是
  source of truth，vec 是检索副本），检索走库内 `<=>` 余弦（C 实现，无上限）。

为什么是"无维度 vector 列 + 库内精确扫描"，而不是 HNSW 索引：
- HNSW 索引要求固定维度（`vector(1024)`），而本项目多 provider 的 embedding
  维度不一（GLM 2048 / Qwen 1024 / OpenAI 1536），混存时固定维度列无法写入。
- 库内精确扫描已是 C 实现，十万级以内规模满足延迟要求；待统一 provider 并
  固定维度后，再执行（二选一）：
    1) 新列 `embedding_vec_1024 vector(1024)` + 回填 +
       CREATE INDEX ... USING hnsw (embedding_vec_1024 vector_cosine_ops)
    2) 或在 embedding_model 统一后直接 ALTER 本列类型为 vector(固定维) 再建索引
- 检索 SQL 中的 `vector_dims(...) = :dim` 过滤保证维度不匹配的行不参与排序。

回填说明：JSONB 数组的文本形式 `[0.1,0.2,...]` 恰好是 pgvector 的字面量格式，
`(embedding #>> '{}')::vector` 一步完成转换（#>> '{}' 取 JSONB 顶层标量文本）。

幂等性：CREATE EXTENSION/ADD COLUMN 均带 IF NOT EXISTS；回填带 WHERE 守卫，
重复执行为 no-op。CREATE EXTENSION 需要超级用户权限 —— compose 的
POSTGRES_USER 默认即超级用户。

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-25
"""
from __future__ import annotations

from typing import Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels = None
depends_on = None


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgresql():
        # SQLite（本地测试）没有 pgvector：vector_store 自动走 Python 降级路径
        return
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(
        """
        ALTER TABLE evidence
            ADD COLUMN IF NOT EXISTS embedding_vec vector
        """
    )
    # 回填存量行（JSONB → vector 文本字面量，一步 CAST）
    op.execute(
        """
        UPDATE evidence
           SET embedding_vec = (embedding #>> '{}')::vector
         WHERE embedding IS NOT NULL
           AND embedding_vec IS NULL
        """
    )


def downgrade() -> None:
    if not _is_postgresql():
        return
    op.execute("ALTER TABLE evidence DROP COLUMN IF EXISTS embedding_vec")
    # 不 DROP EXTENSION vector：可能有同库其他使用者；仅回滚本应用自己的列
