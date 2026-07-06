"""Evidence Service — 证据查询 + 时间线 + Connector 编排 + 语义检索"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

import httpx
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth_middleware import AuthContext, get_db_from_auth, require
from shared.config import settings
from shared.exceptions import ConnectorError, NotFoundError, ValidationError
from shared.models import Evidence, TimelineEvent
from shared.rate_limit import api_limit, write_limit
from shared.schemas import EvidenceOut, Page, TimelineEventOut
from shared.vector_store import search_evidence, SearchResult

router = APIRouter(tags=["evidence"])


@router.get("/evidence", response_model=Page[EvidenceOut], summary="查询证据列表")
@api_limit
async def list_evidence(
    request: Request,
    case_id: Optional[str] = Query(default=None),
    source_system: Optional[str] = Query(default=None, description="LIMS|MES|EMS|CMMS|LMS|BMS"),
    min_confidence: Optional[int] = Query(default=None, ge=1, le=4),
    keyword: Optional[str] = Query(default=None, description="摘要模糊搜索"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db_from_auth),
    _: AuthContext = Depends(require("evidence:read")),
) -> Page[EvidenceOut]:
    """查询证据列表 — 支持多维过滤"""
    stmt = select(Evidence)
    if case_id:
        stmt = stmt.where(Evidence.case_id == case_id)
    if source_system:
        stmt = stmt.where(Evidence.source_system == source_system)
    if min_confidence:
        stmt = stmt.where(Evidence.confidence >= min_confidence)
    if keyword:
        stmt = stmt.where(Evidence.summary.ilike(f"%{keyword}%"))

    # count — 使用 func.count() 而非取全行
    from sqlalchemy import func
    count_stmt = select(func.count()).select_from(Evidence)
    if case_id:
        count_stmt = count_stmt.where(Evidence.case_id == case_id)
    if source_system:
        count_stmt = count_stmt.where(Evidence.source_system == source_system)
    if min_confidence:
        count_stmt = count_stmt.where(Evidence.confidence >= min_confidence)
    if keyword:
        count_stmt = count_stmt.where(Evidence.summary.ilike(f"%{keyword}%"))
    total = (await db.execute(count_stmt)).scalar_one()

    stmt = stmt.order_by(desc(Evidence.collected_at)).offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(stmt)).scalars().all()

    return Page[EvidenceOut](
        items=[EvidenceOut.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/evidence/{evidence_id}", response_model=EvidenceOut, summary="查询证据详情")
@api_limit
async def get_evidence(
    request: Request,
    evidence_id: str,
    db: AsyncSession = Depends(get_db_from_auth),
    _: AuthContext = Depends(require("evidence:read")),
) -> EvidenceOut:
    """查询证据完整信息（含 raw_data）"""
    ev = (await db.execute(select(Evidence).where(Evidence.id == evidence_id))).scalar_one_or_none()
    if not ev:
        raise NotFoundError(f"证据 {evidence_id} 不存在")
    return EvidenceOut.model_validate(ev)


@router.get("/cases/{case_id}/timeline", response_model=List[TimelineEventOut], summary="案例时间线")
@api_limit
async def get_timeline(
    request: Request,
    case_id: str,
    db: AsyncSession = Depends(get_db_from_auth),
    _: AuthContext = Depends(require("evidence:read")),
) -> List[TimelineEventOut]:
    """获取案例的时间线事件 — 按时间排序"""
    rows = (await db.execute(
        select(TimelineEvent)
        .where(TimelineEvent.case_id == case_id)
        .order_by(TimelineEvent.occurred_at, TimelineEvent.sort_order)
    )).scalars().all()
    return [TimelineEventOut.model_validate(r) for r in rows]


@router.post("/evidence/fetch-from-source", summary="从源系统拉取增量证据（触发 Connector）")
@write_limit
async def fetch_from_source(
    request: Request,
    source: str = Query(..., description="源系统代码：LMS|BMS|EMS|MES"),
    case_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db_from_auth),
    auth: AuthContext = Depends(require("evidence:write")),
) -> dict:
    """从源系统 Connector 拉取最新证据

    Phase 1: 仅对接 LMS Mock
    Phase 2 改进6: 新增 BMS / EMS / MES Connector
    """
    if source not in ("LMS", "BMS", "EMS", "MES", "LIMS", "CMMS"):
        raise ConnectorError(f"不支持的源系统: {source}")

    # Connector URL + 默认拉取路径映射（每个 Connector 的入口端点）
    connector_map = {
        "LMS": (settings.lms_connector_url, "/lms/cert-status"),
        "BMS": (settings.bms_connector_url, "/bms/ahus"),
        "EMS": (settings.ems_connector_url, "/ems/exceedances"),
        "MES": (settings.mes_connector_url, "/mes/cctv/anomalies"),
    }
    entry = connector_map.get(source)
    if not entry or not entry[0]:
        # 未配置 Connector URL → 跳过（沿用已有静态证据）
        return {
            "status": "skipped",
            "source": source,
            "message": f"{source} Connector 未配置或暂未实现，请使用已有证据数据",
            "fetched_count": 0,
        }
    base_url, path = entry

    # 查询参数：LMS 用 employee_id 过滤，其他 Connector 不带参数
    params = {}
    if source == "LMS" and case_id:
        params["employee_id"] = "EMP-2187"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(f"{base_url.rstrip('/')}{path}", params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        raise ConnectorError(f"{source} Connector 调用失败: {e}")

    # 统计拉取条数（data 可能是 dict 或 list）—— 优先用 total 字段，其次 items/employees 长度
    if isinstance(data, list):
        fetched_count = len(data)
    elif isinstance(data, dict):
        if "total" in data and isinstance(data["total"], int):
            fetched_count = data["total"]
        elif "items" in data:
            fetched_count = len(data["items"])
        elif "employees" in data:
            fetched_count = len(data["employees"])
        else:
            fetched_count = 1
    else:
        fetched_count = 1

    return {
        "status": "ok",
        "source": source,
        "data": data,
        "fetched_count": fetched_count,
        "triggered_by": auth.email,
    }


# ============================================================
#  语义检索 — Phase 2 新增
# ============================================================
@router.post("/evidence/{evidence_id}/embed", summary="为证据生成并持久化 embedding 向量")
@write_limit
async def embed_evidence(
    request: Request,
    evidence_id: str,
    db: AsyncSession = Depends(get_db_from_auth),
    auth: AuthContext = Depends(require("evidence:write")),
) -> dict:
    """为单条证据计算 embedding 向量并持久化到数据库

    - 调用 LLM provider 的 embedding API（GLM/Qwen/OpenAI）
    - 向量存储在 evidence.embedding 字段（JSONB）
    - LLM 未配置时返回 400 错误，提示配置方法
    """
    ev = (await db.execute(select(Evidence).where(Evidence.id == evidence_id))).scalar_one_or_none()
    if not ev:
        raise NotFoundError(f"证据 {evidence_id} 不存在")

    if not ev.summary:
        raise ValidationError("证据无摘要内容，无法生成 embedding")

    from shared.llm_client import llm_client, LLMError
    try:
        if not llm_client.supports_embeddings:
            return {
                "status": "skipped",
                "evidence_id": evidence_id,
                "message": "LLM embedding 未配置，跳过向量化",
                "embedding_model": None,
            }
        embedding = await llm_client.embed_text(ev.summary)
    except LLMError as e:
        raise ValidationError(f"Embedding 生成失败: {e}")

    ev.embedding = embedding
    ev.embedding_model = llm_client.embedding_model_name
    ev.embedded_at = datetime.now(timezone.utc)
    await db.commit()

    return {
        "status": "ok",
        "evidence_id": evidence_id,
        "embedding_dim": len(embedding),
        "embedding_model": llm_client.embedding_model_name,
        "embedded_at": ev.embedded_at.isoformat(),
    }


@router.post("/evidence/embed-batch", summary="批量生成证据 embedding")
@write_limit
async def embed_evidence_batch(
    request: Request,
    case_id: Optional[str] = Query(default=None, description="仅嵌入指定案例的证据，不传则全部"),
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db_from_auth),
    auth: AuthContext = Depends(require("evidence:write")),
) -> dict:
    """批量嵌入证据（跳过已嵌入的）"""
    from shared.llm_client import llm_client, LLMError

    if not llm_client.supports_embeddings:
        return {
            "status": "skipped",
            "message": "LLM embedding 未配置",
            "embedded_count": 0,
        }

    # 查找未嵌入的证据
    stmt = select(Evidence).where(Evidence.embedding.is_(None)).where(Evidence.summary.isnot(None))
    if case_id:
        stmt = stmt.where(Evidence.case_id == case_id)
    stmt = stmt.limit(limit)
    evidences = (await db.execute(stmt)).scalars().all()

    if not evidences:
        return {"status": "ok", "embedded_count": 0, "message": "无待嵌入证据"}

    # 批量嵌入
    texts = [ev.summary for ev in evidences]
    try:
        embeddings = await llm_client.embed_texts(texts)
    except LLMError as e:
        raise ValidationError(f"批量嵌入失败: {e}")

    now = datetime.now(timezone.utc)
    model_name = llm_client.embedding_model_name
    for ev, emb in zip(evidences, embeddings):
        ev.embedding = emb
        ev.embedding_model = model_name
        ev.embedded_at = now

    await db.commit()

    return {
        "status": "ok",
        "embedded_count": len(evidences),
        "embedding_model": model_name,
        "embedded_at": now.isoformat(),
    }


@router.get("/evidence/search", summary="证据语义搜索")
@api_limit
async def semantic_search(
    request: Request,
    q: str = Query(..., min_length=1, description="查询文本"),
    top_k: int = Query(default=10, ge=1, le=50),
    exclude_case_id: Optional[str] = Query(default=None, description="排除该案例的证据"),
    db: AsyncSession = Depends(get_db_from_auth),
    _: AuthContext = Depends(require("evidence:read")),
) -> dict:
    """证据语义搜索 — 优先向量检索，降级到关键词搜索

    - 若 LLM embedding 可用且查询向量生成成功 → 语义搜索（cosine similarity）
    - 否则降级到关键词搜索（Jaccard 相似度）
    - 排除 exclude_case_id 指定的案例（用于跨案例关联）
    """
    query_embedding = None
    search_mode = "keyword"
    embedding_error = None

    from shared.llm_client import llm_client, LLMError
    if llm_client.supports_embeddings:
        try:
            query_embedding = await llm_client.embed_text(q)
            search_mode = "semantic"
        except LLMError as e:
            embedding_error = str(e)
            # 降级到关键词搜索

    results = await search_evidence(
        db, query=q, top_k=top_k,
        exclude_case_id=exclude_case_id,
        query_embedding=query_embedding,
    )

    return {
        "query": q,
        "search_mode": search_mode,
        "top_k": top_k,
        "exclude_case_id": exclude_case_id,
        "results": [
            {
                "evidence_id": r.evidence_id,
                "case_id": r.case_id,
                "source_system": r.source_system,
                "summary": r.summary,
                "score": round(r.score, 4),
                "matched_by": r.matched_by,
            }
            for r in results
        ],
        "result_count": len(results),
        "embedding_error": embedding_error,
    }


@router.get("/evidence/related-cases", summary="跨案例关联查询")
@api_limit
async def find_related_cases(
    request: Request,
    q: str = Query(..., min_length=1, description="当前案例的关键描述"),
    top_k: int = Query(default=5, ge=1, le=20),
    exclude_case_id: Optional[str] = Query(default=None, description="当前案例 ID，从结果排除"),
    db: AsyncSession = Depends(get_db_from_auth),
    _: AuthContext = Depends(require("evidence:read")),
) -> dict:
    """跨案例关联 — 找出与当前案例描述相似的历史案例

    返回按相关度聚合的案例列表（含证据 ID + 相似度评分）
    """
    from shared.vector_store import find_related_cases as _find_related
    from shared.llm_client import llm_client, LLMError

    query_embedding = None
    embedding_error = None
    if llm_client.supports_embeddings:
        try:
            query_embedding = await llm_client.embed_text(q)
        except LLMError as e:
            embedding_error = str(e)

    related = await _find_related(
        db, query=q, top_k=top_k,
        exclude_case_id=exclude_case_id,
        query_embedding=query_embedding,
    )

    return {
        "query": q,
        "exclude_case_id": exclude_case_id,
        "related_cases": related,
        "related_count": len(related),
        "embedding_error": embedding_error,
    }
