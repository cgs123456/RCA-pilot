"""向量检索 — 纯 Python 余弦相似度（不依赖 numpy/Chroma/faiss/sqlite-vec）

设计目标：
1. **零额外依赖**：纯 Python 实现余弦相似度，带候选数上限
2. **DB 持久化**：embedding 存在 Evidence 表 JSONB 列
3. **降级友好**：LLM 未配置时降级到关键词 Jaccard 搜索
4. **跨案例关联**：支持搜索非当前案例的证据，用于 related_historical_cases

规模提示：候选上限 2000 条适用于中小规模；证据量增长到万级以上时应迁移
pgvector（HNSW 索引）做 ANN 检索。

用法：
    from shared.vector_store import vector_search, keyword_search, search_evidence

    # 语义搜索（自动降级到关键词）
    results = await search_evidence(db, query="HVAC 差压超标", top_k=10)

    # 跨案例关联（排除当前案例）
    related = await search_evidence(
        db, query="资质逾期", top_k=5, exclude_case_id="DEV-001"
    )
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Evidence


# ============================================================
# 数据结构
# ============================================================
@dataclass
class SearchResult:
    """语义搜索结果项"""
    evidence_id: str
    case_id: str | None
    source_system: str
    summary: str
    score: float  # 0.0-1.0，余弦相似度或关键词匹配率
    matched_by: str  # "semantic" | "keyword"


# ============================================================
# 向量检索核心
# ============================================================
def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """计算两个向量的余弦相似度（不依赖 numpy，避免依赖膨胀）

    Returns:
        float: -1.0 到 1.0，但 embedding 通常归一化后为 0.0-1.0
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


async def vector_search(
    db: AsyncSession,
    query_embedding: list[float],
    top_k: int = 10,
    exclude_case_id: str | None = None,
    max_candidates: int = 2000,
) -> list[SearchResult]:
    """向量语义搜索

    Args:
        db: 数据库会话
        query_embedding: 查询向量
        top_k: 返回前 K 条
        exclude_case_id: 排除该案例的证据（用于跨案例关联时排除自身）
        max_candidates: 参与相似度计算的最大候选数（防止全表加载；
            证据量超过此值时应迁移 pgvector + HNSW 索引）

    Returns:
        List[SearchResult]: 按相似度降序排列的结果
    """
    # 候选上限 + 确定性排序（新证据优先参与计算），避免每次搜索全表 IO + O(N·D) CPU
    stmt = (
        select(Evidence)
        .where(Evidence.embedding.isnot(None))
        .order_by(Evidence.created_at.desc())
        .limit(max_candidates)
    )
    if exclude_case_id:
        stmt = stmt.where(Evidence.case_id != exclude_case_id)
    result = await db.execute(stmt)
    evidences = result.scalars().all()

    if not evidences:
        return []

    dim = len(query_embedding)
    # 计算相似度（维度不匹配的向量直接跳过 —— 不同 embedding model 的向量混存时
    # 余弦值无意义，旧实现返回 0 分会污染排序）
    scored = []
    for ev in evidences:
        if not ev.embedding or len(ev.embedding) != dim:
            continue
        score = _cosine_similarity(query_embedding, ev.embedding)
        scored.append(SearchResult(
            evidence_id=ev.id,
            case_id=ev.case_id,
            source_system=ev.source_system,
            summary=ev.summary or "",
            score=score,
            matched_by="semantic",
        ))

    # 按相似度降序，取 top_k
    scored.sort(key=lambda x: x.score, reverse=True)
    return scored[:top_k]


# ============================================================
# 关键词搜索（降级路径）
# ============================================================
async def keyword_search(
    db: AsyncSession,
    query: str,
    top_k: int = 10,
    exclude_case_id: str | None = None,
) -> list[SearchResult]:
    """关键词 LIKE 搜索 — LLM 未配置时的降级路径

    使用简单的 Jaccard 相似度：query 词集与 summary 词集的交集占比
    """
    if not query.strip():
        return []

    # 候选上限 + 确定性排序，避免全表加载（与 vector_search 同策略）
    stmt = (
        select(Evidence)
        .order_by(Evidence.created_at.desc())
        .limit(2000)
    )
    if exclude_case_id:
        stmt = stmt.where(Evidence.case_id != exclude_case_id)
    result = await db.execute(stmt)
    evidences = result.scalars().all()

    # 分词（中文按字符切，英文按空格切）
    def tokenize(text: str) -> set:
        if not text:
            return set()
        # 简单分词：英文按空格，中文按字符
        tokens = set()
        for part in text.lower().split():
            if len(part) > 1:
                tokens.add(part)
        # 中文按 2-gram
        text_clean = text.lower()
        for i in range(len(text_clean) - 1):
            tokens.add(text_clean[i:i + 2])
        return tokens

    query_tokens = tokenize(query)
    if not query_tokens:
        return []

    scored = []
    for ev in evidences:
        ev_tokens = tokenize(ev.summary or "")
        if not ev_tokens:
            continue
        # Jaccard 相似度
        intersection = len(query_tokens & ev_tokens)
        union = len(query_tokens | ev_tokens)
        score = intersection / union if union > 0 else 0.0
        if score > 0:
            scored.append(SearchResult(
                evidence_id=ev.id,
                case_id=ev.case_id,
                source_system=ev.source_system,
                summary=ev.summary or "",
                score=score,
                matched_by="keyword",
            ))

    scored.sort(key=lambda x: x.score, reverse=True)
    return scored[:top_k]


# ============================================================
# 统一入口（自动降级）
# ============================================================
async def search_evidence(
    db: AsyncSession,
    query: str,
    top_k: int = 10,
    exclude_case_id: str | None = None,
    query_embedding: list[float] | None = None,
) -> list[SearchResult]:
    """证据搜索 — 优先语义搜索，降级到关键词搜索

    Args:
        db: 数据库会话
        query: 查询文本（用于关键词搜索兜底）
        top_k: 返回前 K 条
        exclude_case_id: 排除该案例
        query_embedding: 查询向量（None 时走关键词搜索）

    Returns:
        List[SearchResult]
    """
    if query_embedding:
        results = await vector_search(db, query_embedding, top_k, exclude_case_id)
        if results:
            return results
        # 向量为空或无结果时降级
    return await keyword_search(db, query, top_k, exclude_case_id)


# ============================================================
# 跨案例关联
# ============================================================
async def find_related_cases(
    db: AsyncSession,
    query: str,
    top_k: int = 5,
    exclude_case_id: str | None = None,
    query_embedding: list[float] | None = None,
) -> list[dict]:
    """跨案例关联 — 找出与当前案例相关的历史案例

    Args:
        db: 数据库会话
        query: 当前案例的关键描述
        top_k: 返回前 K 个相关案例
        exclude_case_id: 排除当前案例
        query_embedding: 查询向量

    Returns:
        List[dict]: [{"case_id": "DEV-XXX", "evidence_ids": [...], "score": 0.85}, ...]
    """
    results = await search_evidence(
        db, query, top_k=top_k * 3,  # 取更多证据后再按案例聚合
        exclude_case_id=exclude_case_id,
        query_embedding=query_embedding,
    )

    # 按案例聚合
    case_map: dict = {}
    for r in results:
        if r.case_id is None or r.case_id == exclude_case_id:
            continue
        if r.case_id not in case_map:
            case_map[r.case_id] = {
                "case_id": r.case_id,
                "evidence_ids": [],
                "max_score": r.score,
                "matched_by": r.matched_by,
                "summaries": [],
            }
        case_map[r.case_id]["evidence_ids"].append(r.evidence_id)
        case_map[r.case_id]["summaries"].append(r.summary)
        if r.score > case_map[r.case_id]["max_score"]:
            case_map[r.case_id]["max_score"] = r.score

    # 按最高相似度排序，取 top_k
    related = sorted(
        case_map.values(),
        key=lambda x: x["max_score"],
        reverse=True,
    )[:top_k]
    related = cast(list[dict], related)

    # 清理输出
    for item in related:
        item["evidence_count"] = len(item["evidence_ids"])
        item["sample_summaries"] = item["summaries"][:3]  # 只展示前 3 条证据摘要
        del item["summaries"]

    return related
