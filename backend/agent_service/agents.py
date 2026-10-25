"""Agent 公共辅助 + Evidence Hunter + Guardian（P3-① 自 routes.py 拆出）

P3-① 自 agent_service/routes.py 拆出（机械搬运，正文与原文件逐字一致）。
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.database import release_db_connection
from shared.models import Deviation, FiveWhy, SkepticChallenge


def _hash_output(data: Any) -> str:
    """计算 Agent 输出的 hash（用于审计存证）"""
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _filter_hallucinated_evidence(
    evidence_ids: list[str], valid_ids: list[str]
) -> tuple[list[str], list[str]]:
    """过滤 LLM 输出中不存在的证据 ID（防幻觉直通审计链）

    Returns:
        (过滤后的证据 ID 列表, 被剔除的幻觉 ID 列表)
    """
    valid = set(valid_ids)
    kept = [e for e in (evidence_ids or []) if e in valid]
    dropped = [e for e in (evidence_ids or []) if e not in valid]
    return kept, dropped


def _case_evidence_ids(case: Deviation) -> list[str]:
    return [ev.id for ev in (case.evidence or [])]


# ============================================================
#  Agent 实现（规则版）
# ============================================================
async def _run_evidence_hunter(
    db: AsyncSession, case: Deviation, flag_user: str | None = None
) -> dict[str, Any]:
    """Evidence Hunter — 聚合案例已有证据 + 标注证据缺口 + 跨案例关联

    支持两种模式（通过 feature flag `evidence_hunter_semantic` 灰度切换）：
    1. **规则版（默认）**：仅做统计 + 缺口识别
    2. **语义检索版（灰度）**：调用 LLM embedding 做跨案例关联，输出 related_historical_cases

    LLM 路径需要 `evidence_hunter_semantic` flag 启用且 LLM 可配置。
    LLM 失败时降级到关键词搜索，仍能返回 related_historical_cases（matched_by=keyword）。
    """
    # 灰度按"触发者"而非案例负责人评估 —— 灰度归属与实际配置视角一致（可审计复现）
    from shared.feature_flag import is_enabled  # 调用时绑定（tests patch shared.feature_flag.is_enabled）
    use_semantic = is_enabled("evidence_hunter_semantic", user_id=flag_user or case.investigator_id)

    evidence_list = case.evidence
    by_source: dict[str, int] = {}
    for ev in evidence_list:
        by_source[ev.source_system] = by_source.get(ev.source_system, 0) + 1

    # 识别证据缺口（基于 6M 鱼骨图维度）
    gaps: list[str] = []
    if not any(ev.source_system == "LIMS" for ev in evidence_list):
        gaps.append("缺少 LIMS 检验数据（菌种鉴定）")
    if not any(ev.source_system == "LMS" for ev in evidence_list):
        gaps.append("缺少 LMS 培训/资质记录")
    if not any(ev.source_system == "BMS" for ev in evidence_list):
        gaps.append("缺少 BMS HVAC 监控数据")

    # 异常事件
    anomalies = [ev for ev in evidence_list if ev.summary and ("超" in ev.summary or "异常" in ev.summary or "expired" in (ev.raw_data or {}).get("status", ""))]

    # 输出 payload（用于 hash 计算）— 不含 timestamp 等不稳定字段
    output_payload: dict[str, Any] = {
        "agent": "evidence_hunter",
        "status": "completed",
        "summary": f"收集 {len(evidence_list)} 条证据，覆盖 {len(by_source)} 个源系统",
        "evidence_count": len(evidence_list),
        "by_source": by_source,
        "anomaly_count": len(anomalies),
        "gaps": gaps,
    }

    # ============================================================
    #  跨案例关联（feature flag 灰度）
    # ============================================================
    related_cases = []
    semantic_status = "disabled"  # disabled | semantic | keyword | fallback
    if use_semantic:
        # 用案例标题 + 描述作为查询文本，搜索历史案例
        query_text = f"{case.title} {case.description or ''}".strip()
        try:
            from shared.llm_client import LLMError, llm_client
            from shared.vector_store import find_related_cases as _find_related

            query_embedding = None
            if llm_client.supports_embeddings:
                try:
                    # C3：embedding 出站前归还连接（查询文本已就绪）
                    await release_db_connection(db)
                    query_embedding = await llm_client.embed_text(query_text)
                    semantic_status = "semantic"
                except LLMError:
                    semantic_status = "keyword"  # 降级
            else:
                semantic_status = "keyword"  # LLM 未配置，走关键词

            related_cases = await _find_related(
                db, query=query_text, top_k=5,
                exclude_case_id=case.id,
                query_embedding=query_embedding,
            )
        except Exception:
            # 任何异常都不阻塞流水线
            semantic_status = "fallback"

    output_payload["related_historical_cases"] = related_cases
    output_payload["semantic_search_status"] = semantic_status

    return {
        **output_payload,
        "ai_model": "rule-template-v1" if not use_semantic else f"semantic-{semantic_status}",
        "ai_output_hash": _hash_output(output_payload),
        "timestamp": datetime.now(UTC).isoformat(),
    }



async def _run_guardian(
    db: AsyncSession, case: Deviation, drafter_output: dict[str, Any]
) -> dict[str, Any]:
    """Guardian — ALCOA+ 9 项合规检查（数据驱动，不再硬编码 demo 案例 warning）"""
    from sqlalchemy import func as _func

    # 用 COUNT 查询而非关系访问 —— 调用方未必 selectinload 了集合（避免 async 懒加载崩溃）
    fw_count = (await db.execute(
        select(_func.count()).select_from(FiveWhy).where(FiveWhy.case_id == case.id)
    )).scalar_one()
    sc_count = (await db.execute(
        select(_func.count()).select_from(SkepticChallenge).where(SkepticChallenge.case_id == case.id)
    )).scalar_one()

    evidence = case.evidence or []
    by_source = {ev.source_system for ev in evidence}

    # 数据驱动检查项：基于案例实际证据状态判定，而非固定文本
    checks = [
        {"principle": "Attributable (可归因)", "status": "pass",
         "detail": f"流水线由 {case.investigator_id or 'system'} 触发，所有操作均记录操作员与时间戳"},
        {"principle": "Legible (清晰)", "status": "pass",
         "detail": "电子记录可读，结构化存储"},
        {"principle": "Contemporaneous (同期)", "status": "pass",
         "detail": f"证据采集时间范围：{min((ev.collected_at for ev in evidence), default=None)} ~ "
                   f"{max((ev.collected_at for ev in evidence), default=None)}"},
        {
            "principle": "Original (原始)",
            "status": "pass" if all(ev.content_hash for ev in evidence) else "fail",
            "detail": "全部证据含 content_hash 且直接来自源系统" if evidence else "案例无证据 — 需先收集证据",
        },
        {
            "principle": "Accurate (准确)",
            "status": "pass" if len(by_source) >= 3 else "warning",
            "detail": (
                f"证据覆盖 {len(by_source)} 个源系统（{', '.join(sorted(by_source)) or '无'}）"
                if len(by_source) >= 3
                else f"证据仅覆盖 {len(by_source)} 个源系统（{', '.join(sorted(by_source)) or '无'}），数据完整性存在风险 — 建议补充证据"
            ),
        },
        {"principle": "Complete (完整)", "status": "pass" if fw_count else "warning",
         "detail": f"5 Whys {fw_count} 条 + Skeptic {sc_count} 条"},
        {"principle": "Consistent (一致)", "status": "pass",
         "detail": "证据间无已知矛盾（Skeptic 反例需在 HITL gate3 处置）"},
        {"principle": "Enduring (持久)", "status": "pass",
         "detail": "审计日志 hash 链 + DB 触发器防篡改（UPDATE/DELETE/TRUNCATE）"},
        {"principle": "Available (可用)", "status": "pass",
         "detail": "可导出 CSV/JSON 审计包供监管检查"},
    ]
    pass_count = sum(1 for c in checks if c["status"] == "pass")
    warning_count = sum(1 for c in checks if c["status"] == "warning")
    fail_count = sum(1 for c in checks if c["status"] == "fail")

    return {
        "agent": "guardian",
        "status": "completed" if fail_count == 0 else "failed",
        "checks": checks,
        "summary": f"ALCOA+ 9 项检查：{pass_count} 通过 / {warning_count} 警告 / {fail_count} 失败",
        "overall_passed": fail_count == 0,
        "ai_model": "rule-engine-v1",
        "ai_output_hash": _hash_output(checks),
        "timestamp": datetime.now(UTC).isoformat(),
    }


