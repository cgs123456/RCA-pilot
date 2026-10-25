"""Evidence Service — 证据查询 + 时间线 + Connector 编排（落库去重）+ 语义检索

注意：本文件不使用 `from __future__ import annotations`（PEP 563）——
slowapi 包装端点的字符串注解解析会落到 slowapi 模块命名空间导致 NameError。
"""
import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import desc, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.audit import append_audit_log
from shared.auth_middleware import AuthContext, get_db_from_auth, require
from shared.config import settings
from shared.database import release_db_connection
from shared.exceptions import ConnectorError, NotFoundError, ValidationError
from shared.models import Deviation, Evidence, TimelineEvent
from shared.rate_limit import api_limit, write_limit
from shared.schemas import EvidenceOut, Page, TimelineEventOut
from shared.vector_store import search_evidence

router = APIRouter(tags=["evidence"])

# 单次 embedding API 请求的文本上限（DashScope/GLM/OpenAI 兼容接口的稳妥值）
EMBED_BATCH_SIZE = 64


@router.get("/evidence", response_model=Page[EvidenceOut], summary="查询证据列表")
@api_limit
async def list_evidence(
    request: Request,
    case_id: str | None = Query(default=None),
    source_system: str | None = Query(default=None, description="LIMS|MES|EMS|CMMS|LMS|BMS"),
    min_confidence: int | None = Query(default=None, ge=1, le=4),
    keyword: str | None = Query(default=None, description="摘要模糊搜索"),
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


@router.get("/cases/{case_id}/timeline", response_model=list[TimelineEventOut], summary="案例时间线")
@api_limit
async def get_timeline(
    request: Request,
    case_id: str,
    db: AsyncSession = Depends(get_db_from_auth),
    _: AuthContext = Depends(require("evidence:read")),
) -> list[TimelineEventOut]:
    """获取案例的时间线事件 — 按时间排序"""
    rows = (await db.execute(
        select(TimelineEvent)
        .where(TimelineEvent.case_id == case_id)
        .order_by(TimelineEvent.occurred_at, TimelineEvent.sort_order)
    )).scalars().all()
    return [TimelineEventOut.model_validate(r) for r in rows]


# ============================================================
#  Connector 编排（拉取 → 去重 → 落库 → 审计）
# ============================================================
def _content_hash(source: str, item: Any) -> str:
    """对证据内容做 SHA-256 —— 内容寻址去重的依据"""
    canonical = json.dumps({"source": source, "item": item}, ensure_ascii=False, sort_keys=True, default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _extract_items(data: Any) -> list[dict[str, Any]]:
    """把 Connector 的响应体归一化为证据条目列表"""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("items", "employees", "exceedances", "anomalies", "ahus", "batches", "records"):
            v = data.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
        return [data]  # 单对象响应
    return []


def _summary_of(source: str, item: dict[str, Any]) -> str:
    """从条目提取可读摘要（按各 Connector 常见字段名）"""
    for key in ("summary", "alert", "message", "description", "course_name", "anomaly_type", "exceedance_type"):
        v = item.get(key)
        if isinstance(v, str) and v.strip():
            return f"[{source}] {v.strip()}"
    # 兜底：取前 3 个字段拼接
    parts = [f"{k}={v}" for k, v in list(item.items())[:3]]
    return f"[{source}] " + ", ".join(parts) if parts else f"[{source}] 证据条目"


def _collected_at_of(item: dict[str, Any]) -> datetime:
    for key in ("timestamp", "collected_at", "detected_at", "occurred_at", "recorded_at", "created_at"):
        v = item.get(key)
        if isinstance(v, str) and v:
            try:
                return datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                continue
    return datetime.now(UTC)


async def _derive_employee_id(db: AsyncSession, case_id: str | None) -> str | None:
    """从案例已有 LMS 证据推导员工号 —— 不再硬编码 EMP-2187"""
    if not case_id:
        return None
    rows = (await db.execute(
        select(Evidence)
        .where(Evidence.case_id == case_id)
        .where(Evidence.source_system == "LMS")
        .order_by(desc(Evidence.collected_at))
        .limit(5)
    )).scalars().all()
    for ev in rows:
        rd = ev.raw_data if isinstance(ev.raw_data, dict) else {}
        for key in ("employee_id", "employeeId", "emp_id"):
            v = rd.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        # 嵌套结构兜底
        for nested in rd.values():
            if isinstance(nested, dict):
                for key in ("employee_id", "employeeId", "emp_id"):
                    v = nested.get(key)
                    if isinstance(v, str) and v.strip():
                        return v.strip()
    return None


async def _persist_fetched_evidence(
    db: AsyncSession,
    *,
    source: str,
    case_id: str | None,
    items: list[dict[str, Any]],
) -> int:
    """证据落库（content_hash 去重，内容寻址 ID 天然幂等）。返回新增条数。

    并发安全：两条相同内容的并发拉取会得到相同 ID（E-{source}-{hash}），
    exists 检查拦不住检查窗口内的竞态 —— 用 SAVEPOINT 捕获主键冲突后幂等跳过，
    而不是让事务末尾的 commit 炸出 500。
    """
    new_count = 0
    now = datetime.now(UTC)
    seen_hashes: set[str] = set()  # 批内去重：同一响应里重复的条目只落一次
    for item in items:
        chash = _content_hash(source, item)
        if chash in seen_hashes:
            continue
        seen_hashes.add(chash)
        ev_id = f"E-{source}-{chash.split(':', 1)[1][:10]}"
        exists = (await db.execute(
            select(func.count()).select_from(Evidence).where(Evidence.id == ev_id)
        )).scalar_one()
        if exists:
            continue
        try:
            async with db.begin_nested():
                db.add(Evidence(
                    id=ev_id,
                    case_id=case_id,
                    source_system=source,
                    collected_at=_collected_at_of(item),
                    summary=_summary_of(source, item),
                    raw_data=item,
                    content_hash=chash,
                    confidence=3,
                    is_signed=False,
                    created_at=now,
                ))
                await db.flush()
        except IntegrityError:
            # 并发竞态：另一请求已插入同 ID（内容相同）—— 幂等跳过
            continue
        new_count += 1
    return new_count


@router.post("/evidence/fetch-from-source", summary="从源系统拉取增量证据（触发 Connector 并落库）")
@write_limit
async def fetch_from_source(
    request: Request,
    source: str = Query(..., description="源系统代码：LMS|BMS|EMS|MES"),
    case_id: str | None = None,
    db: AsyncSession = Depends(get_db_from_auth),
    auth: AuthContext = Depends(require("evidence:write")),
) -> dict:
    """从源系统 Connector 拉取最新证据

    Phase 1: 仅对接 LMS Mock
    Phase 2 改进6: 新增 BMS / EMS / MES Connector

    拉取后的证据会 content_hash 去重落库（GxP: Original + 完整性），并写入审计日志。
    """
    if source not in ("LMS", "BMS", "EMS", "MES", "LIMS", "CMMS"):
        raise ConnectorError(f"不支持的源系统: {source}")

    # case_id 必须存在 —— 否则证据带悬挂外键落库，事务末尾才炸 500
    if case_id is not None:
        case_exists = (
            await db.execute(select(Deviation.id).where(Deviation.id == case_id))
        ).scalar_one_or_none()
        if case_exists is None:
            raise NotFoundError(f"案例 {case_id} 不存在")

    # Connector URL + 默认拉取路径映射（每个 Connector 的入口端点）。
    # 路径必须与 connectors/{lms,bms,ems,mes}/routes.py 的实际挂载路径
    # （/api/v1/{prefix}/...）逐字一致 —— 历史上漏过 /api/v1 前缀导致线上 404。
    connector_map = {
        "LMS": (settings.lms_connector_url, "/api/v1/lms/cert-status"),
        "BMS": (settings.bms_connector_url, "/api/v1/bms/ahus"),
        "EMS": (settings.ems_connector_url, "/api/v1/ems/exceedances"),
        "MES": (settings.mes_connector_url, "/api/v1/mes/cctv/anomalies"),
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

    # 查询参数：LMS 用 employee_id 过滤 —— 从案例已有证据推导，不硬编码
    params: dict[str, Any] = {}
    if source == "LMS":
        employee_id = await _derive_employee_id(db, case_id)
        if employee_id:
            params["employee_id"] = employee_id
    if source == "MES" and case_id:
        params["case_id"] = case_id

    try:
        # 服务间调用鉴权（connector 端点要求 X-Service-Key；dev 未配置时省略）
        headers = {"X-Service-Key": settings.service_api_key} if settings.service_api_key else {}
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(f"{base_url.rstrip('/')}{path}", params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        raise ConnectorError(f"{source} Connector 调用失败: {e}") from e

    items = _extract_items(data)
    fetched_count = len(items)

    # 落库 + 审计（拉取的证据成为平台 Original 记录，必须可追溯）
    new_count = await _persist_fetched_evidence(db, source=source, case_id=case_id, items=items)
    await append_audit_log(
        db,
        operator=auth.email,
        action="EVIDENCE_FETCHED",
        target=f"{source}/{case_id}" if case_id else source,
        detail=(
            f"从 {source} Connector 拉取 {fetched_count} 条，新增落库 {new_count} 条"
            f"（content_hash 去重）"
        ),
        ip_address=auth.ip_address,
        session_id=auth.session_id,
        evidence_refs=[],
    )

    return {
        "status": "ok",
        "source": source,
        "case_id": case_id,
        "data": data,
        "fetched_count": fetched_count,
        "persisted_count": new_count,
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
    - 变更写入审计日志（GxP: 证据元数据变更可追溯）
    - LLM 未配置时返回 400 错误，提示配置方法
    """
    ev = (await db.execute(select(Evidence).where(Evidence.id == evidence_id))).scalar_one_or_none()
    if not ev:
        raise NotFoundError(f"证据 {evidence_id} 不存在")

    if not ev.summary:
        raise ValidationError("证据无摘要内容，无法生成 embedding")

    from shared.llm_client import LLMError, llm_client
    try:
        if not llm_client.supports_embeddings:
            return {
                "status": "skipped",
                "evidence_id": evidence_id,
                "message": "LLM embedding 未配置，跳过向量化",
                "embedding_model": None,
            }
        # C3：embedding API 调用可能耗时数秒，先归还 DB 连接
        # （ev 已读入内存，expire_on_commit=False 保证提交后属性仍可读写）
        await release_db_connection(db)
        embedding = await llm_client.embed_text(ev.summary)
    except LLMError as e:
        raise ValidationError(f"Embedding 生成失败: {e}") from e

    ev.embedding = embedding
    ev.embedding_vec = embedding  # pgvector 检索副本（bind_expression 自带 CAST）
    ev.embedding_model = llm_client.embedding_model_name
    ev.embedded_at = datetime.now(UTC)

    # 证据行被变更 —— 必须写审计（旧行为裸 commit 无审计，ALCOA+ 违规）
    await append_audit_log(
        db,
        operator=auth.email,
        action="EVIDENCE_EMBEDDED",
        target=evidence_id,
        detail=(
            f"证据向量化：model={llm_client.embedding_model_name}, dim={len(embedding)}"
        ),
        ip_address=auth.ip_address,
        session_id=auth.session_id,
    )

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
    case_id: str | None = Query(default=None, description="仅嵌入指定案例的证据，不传则全部"),
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db_from_auth),
    auth: AuthContext = Depends(require("evidence:write")),
) -> dict:
    """批量嵌入证据

    - 分批 ≤64 条调用 embedding API（超过部分 provider 会直接 400）
    - 只嵌入 embedding_model 为空或与当前模型不同的证据（切换模型后重嵌，避免
      不同维度向量混存导致余弦恒为 0）
    - FOR UPDATE SKIP LOCKED（PG）防止并发批量请求重复嵌入同一批
    """
    from shared.llm_client import LLMError, llm_client

    if not llm_client.supports_embeddings:
        return {
            "status": "skipped",
            "message": "LLM embedding 未配置",
            "embedded_count": 0,
        }

    model_name = llm_client.embedding_model_name

    # C3 逐批短事务：每轮 SELECT（SKIP LOCKED 行锁）→ embed → 写入 → commit，
    # 行锁与池连接的持有时间从「全量 limit(≤500) 行 + 全部 embed 耗时」缩到「单批 ≤64 行」；
    # commit 后下一轮的 or_ 条件自然排除已嵌入行，循环必然收敛。
    # LLM await 期间仍持有该批行锁（先锁后算防重复嵌入），但范围仅 ≤64 行。
    now = datetime.now(UTC)
    processed = 0
    max_rounds = (limit + EMBED_BATCH_SIZE - 1) // EMBED_BATCH_SIZE + 1
    for _ in range(max_rounds):
        batch_size = min(EMBED_BATCH_SIZE, limit - processed)
        stmt = (
            select(Evidence)
            .where(Evidence.summary.isnot(None))
            .where(
                or_(
                    Evidence.embedding.is_(None),
                    Evidence.embedding_model.is_(None),
                    Evidence.embedding_model != model_name,
                )
            )
            .order_by(Evidence.created_at.desc())
        )
        if case_id:
            stmt = stmt.where(Evidence.case_id == case_id)
        stmt = stmt.limit(batch_size)
        # PG 行锁：并发批量请求不会重复嵌入同一批；SQLite（测试）忽略 FOR UPDATE
        stmt = stmt.with_for_update(skip_locked=True)
        evidences = (await db.execute(stmt)).scalars().all()

        if not evidences:
            break

        chunk = [s for s in (ev.summary for ev in evidences) if s is not None]
        try:
            embeddings = await llm_client.embed_texts(chunk)
        except LLMError as e:
            raise ValidationError(
                f"批量嵌入失败（第 {processed // EMBED_BATCH_SIZE + 1} 批）: {e}"
            ) from e

        for ev, emb in zip(evidences, embeddings, strict=False):
            ev.embedding = emb
            ev.embedding_vec = emb  # pgvector 检索副本（bind_expression 自带 CAST）
            ev.embedding_model = model_name
            ev.embedded_at = now

        # 提交本批：写入 + 释放行锁 + 归还池连接（expire_on_commit=False，对象属性保留）
        await db.commit()
        processed += len(evidences)

    if processed == 0:
        return {"status": "ok", "embedded_count": 0, "message": "无待嵌入证据"}

    await append_audit_log(
        db,
        operator=auth.email,
        action="EVIDENCE_EMBED_BATCH",
        target=case_id or "*",
        detail=f"批量向量化 {processed} 条证据（model={model_name}）",
        ip_address=auth.ip_address,
        session_id=auth.session_id,
    )

    return {
        "status": "ok",
        "embedded_count": processed,
        "embedding_model": model_name,
        "embedded_at": now.isoformat(),
    }


@router.get("/evidence/search", summary="证据语义搜索")
@api_limit
async def semantic_search(
    request: Request,
    q: str = Query(..., min_length=1, description="查询文本"),
    top_k: int = Query(default=10, ge=1, le=50),
    exclude_case_id: str | None = Query(default=None, description="排除该案例的证据"),
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

    from shared.llm_client import LLMError, llm_client
    if llm_client.supports_embeddings:
        try:
            # C3：查询向量生成可能耗时数秒，先归还 DB 连接（本端点只读）
            await release_db_connection(db)
            query_embedding = await llm_client.embed_text(q)
            search_mode = "semantic"
        except LLMError as e:
            embedding_error = str(e)
            # 降级到关键词搜索

    results = await search_evidence(
        db, query=q, top_k=top_k,
        exclude_case_id=exclude_case_id,
        query_embedding=query_embedding,
        embedding_model=llm_client.embedding_model_name,
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
    exclude_case_id: str | None = Query(default=None, description="当前案例 ID，从结果排除"),
    db: AsyncSession = Depends(get_db_from_auth),
    _: AuthContext = Depends(require("evidence:read")),
) -> dict:
    """跨案例关联 — 找出与当前案例描述相似的历史案例

    返回按相关度聚合的案例列表（含证据 ID + 相似度评分）
    """
    from shared.llm_client import LLMError, llm_client
    from shared.vector_store import find_related_cases as _find_related

    query_embedding = None
    embedding_error = None
    if llm_client.supports_embeddings:
        try:
            # C3：查询向量生成前归还 DB 连接（本端点只读）
            await release_db_connection(db)
            query_embedding = await llm_client.embed_text(q)
        except LLMError as e:
            embedding_error = str(e)

    related = await _find_related(
        db, query=q, top_k=top_k,
        exclude_case_id=exclude_case_id,
        query_embedding=query_embedding,
        embedding_model=llm_client.embedding_model_name,
    )

    return {
        "query": q,
        "exclude_case_id": exclude_case_id,
        "related_cases": related,
        "related_count": len(related),
        "embedding_error": embedding_error,
    }
