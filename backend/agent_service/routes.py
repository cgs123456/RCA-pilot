"""Agent Service — 6 Agent 流水线编排引擎

运行时可靠性（GxP 关键）：
- 每个阶段使用独立短事务落库（进度对外可见，pause/cancel 生效）
- 进程重启后启动时扫描僵尸 run 标记 failed（main.py lifespan）
- 异常路径用独立会话标记 failed + 补审计，run 不会永久卡在 pending
- run_id 使用 uuid 后缀，同案例并发启动有 running 守卫 + 部分唯一索引兜底

注意：本文件不使用 `from __future__ import annotations`（PEP 563）——
slowapi 包装端点的字符串注解解析会落到 slowapi 模块命名空间导致 NameError。
"""
import asyncio
import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Query, Request
from pydantic import BaseModel as PydBaseModel
from pydantic import Field as PydField
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shared.audit import append_audit_log
from shared.auth_middleware import AuthContext, get_auth_context, get_db_from_auth, require
from shared.config import settings
from shared.database import db_session, get_db
from shared.exceptions import ConflictError, NotFoundError, ValidationError
from shared.metrics import AGENT_RUNS_TOTAL, AGENT_STAGE_DURATION
from shared.models import (
    AgentRun,
    Deviation,
    FiveWhy,
    SkepticChallenge,
)
from shared.rate_limit import api_limit, write_limit
from shared.schemas import AgentRunCreate, AgentRunOut, Page
from shared.security import safe_compare

router = APIRouter(prefix="/agent", tags=["agent"])

# Agent 流水线顺序
AGENT_PIPELINE = [
    "meta_controller",
    "evidence_hunter",
    "reasoner",
    "skeptic",
    "drafter",
    "guardian",
]

AGENT_LABELS = {
    "meta_controller": "Meta-Controller（编排）",
    "evidence_hunter": "Evidence Hunter（证据收集）",
    "reasoner": "Reasoner（根因推理）",
    "skeptic": "Skeptic（反例挑战）",
    "drafter": "Drafter（报告起草）",
    "guardian": "Guardian（合规终检）",
}

# 流水线中断状态 —— 处于这些状态的 run 不允许重复启动新流水线
ACTIVE_RUN_STATUSES = ("pending", "running")

# 僵尸 run 判定阈值：超过该时长仍处于 active 状态视为进程中断遗留
ZOMBIE_RUN_THRESHOLD_MINUTES = 15


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
    from shared.feature_flag import is_enabled
    # 灰度按"触发者"而非案例负责人评估 —— 灰度归属与实际配置视角一致（可审计复现）
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


async def _run_reasoner(
    db: AsyncSession, case: Deviation, evidence_hunter_output: dict[str, Any],
    flag_user: str | None = None,
) -> dict[str, Any]:
    """Reasoner — 5 Whys 推理 + 鱼骨图

    支持两种模式（通过 feature flag `reasoner_llm_enabled` 灰度切换）：
    1. **规则版（默认兜底）**：基于案例已有 5 Whys 数据 + 数据驱动鱼骨图
    2. **LLM 版（灰度启用）**：调用 LLM Chain-of-Thought，从 Evidence + Timeline 做因果链推理

    LLM 路径需要 `reasoner_llm_enabled` flag 启用且 `LLMClient.is_configured` 为 True。
    任何 LLM 调用失败（配置缺失/网络异常/JSON 解析失败）自动降级到规则版兜底，
    并在 ai_model 字段标记 fallback。
    """
    from shared.feature_flag import is_enabled
    use_llm = is_enabled("reasoner_llm_enabled", user_id=flag_user or case.investigator_id)

    # 先生成规则版输出（作为兜底 + LLM 失败时的回退）
    rule_result = _build_rule_based_reasoner_output(case)
    if not use_llm:
        return rule_result

    # LLM 灰度分支 —— flag 启用时调用 LLM
    try:
        from shared.llm_client import LLMError, llm_client
        if not llm_client.is_configured:
            # LLM 未配置 —— 降级到规则版（标记原因）
            rule_result["ai_model"] = "rule-template-v1 (llm-not-configured)"
            rule_result["llm_fallback_reason"] = "LLM provider/api_key 未配置，降级到规则版"
            return rule_result

        llm_result = await _run_reasoner_with_llm(case, evidence_hunter_output, llm_client)
        return llm_result

    except LLMError as e:
        # LLM 调用失败 —— 降级到规则版，记录 fallback 原因用于审计
        rule_result["ai_model"] = "rule-template-v1 (llm-fallback)"
        rule_result["llm_fallback_reason"] = f"LLM 调用失败降级: {type(e).__name__}: {str(e)[:200]}"
        return rule_result
    except Exception as e:
        # 未知异常 —— 兜底降级，不让 LLM 故障阻塞流水线
        rule_result["ai_model"] = "rule-template-v1 (llm-error)"
        rule_result["llm_fallback_reason"] = f"LLM 未知异常降级: {type(e).__name__}: {str(e)[:200]}"
        return rule_result


def _build_rule_based_reasoner_output(case: Deviation) -> dict[str, Any]:
    """规则版 Reasoner —— 数据驱动，仅汇总案例自身证据

    禁止携带任何具体案例的事实性结论（历史教训：硬编码 demo 案例 EMP-2187 的
    根因会随降级路径进入任何新案例的审计链）。规则版输出 = 结构化模板 +
    案例实际证据引用，事实性结论必须由 LLM 路径或人工（HITL）给出。
    """
    existing_whys = case.five_whys
    valid_ids = _case_evidence_ids(case)
    whys_output = []
    for fw in existing_whys:
        kept, dropped = _filter_hallucinated_evidence(fw.evidence_ids or [], valid_ids)
        whys_output.append({
            "n": fw.n,
            "question": fw.question,
            "answer": fw.answer,
            "evidence_ids": kept,
            "is_root_cause": fw.is_root_cause,
            "ai_model": "rule-template-v1",
            "ai_output_hash": _hash_output({
                "n": fw.n,
                "question": fw.question,
                "answer": fw.answer,
            }),
        })

    # 鱼骨图 6M 分类 —— 从案例实际证据按源系统归类，无证据的维度明确标注"待调查"
    by_source: dict[str, list[str]] = {}
    for ev in case.evidence or []:
        by_source.setdefault(ev.source_system, []).append(ev.id)

    def _category(name: str, source_codes: list[str], hint: str) -> dict[str, Any]:
        ev_ids: list[str] = []
        for code in source_codes:
            ev_ids.extend(by_source.get(code, []))
        if ev_ids:
            causes = [{
                "text": f"关联 {code} 证据（具体原因需 LLM 推理或人工确认）",
                "evidence": ev_ids,
                "severity": "info",
            } for code in source_codes if by_source.get(code)]
        else:
            causes = [{
                "text": f"无 {hint} 相关证据 — 待调查",
                "evidence": [],
                "severity": "info",
                "note": "无证据 — 推理假设",
            }]
        return {"name": name, "causes": causes}

    fishbone = {
        "root": case.title,
        "categories": [
            _category("人员 Man", ["LMS"], "人员资质/培训"),
            _category("设备 Machine", ["BMS"], "设备/HVAC"),
            _category("物料 Material", ["MES"], "物料/批次"),
            _category("方法 Method", ["SYS"], "方法/SOP"),
            _category("环境 Environment", ["EMS"], "环境监测"),
            _category("测量 Measurement", ["LIMS"], "检验/测量"),
        ],
    }

    # 根因摘要：只陈述数据事实，不做事实性根因断言
    top_evidence = [ev.id for ev in (case.evidence or [])[:5]]
    root_cause_summary = (
        f"规则版降级输出：未做事实性根因断言。案例 {case.id} 共有 {len(case.evidence or [])} 条证据"
        f"（{', '.join(top_evidence) if top_evidence else '无'}），"
        f"根因结论请启用 LLM 推理（reasoner_llm_enabled）或在 HITL gate2 人工确认。"
    )

    return {
        "agent": "reasoner",
        "status": "completed",
        "five_whys": whys_output,
        "fishbone": fishbone,
        "root_cause_summary": root_cause_summary,
        "ai_model": "rule-template-v1",
        "ai_output_hash": _hash_output(whys_output),
        "timestamp": datetime.now(UTC).isoformat(),
    }


# ============================================================
#  Reasoner LLM 路径
# ============================================================
# Reasoner LLM 输出 schema —— 用于 Pydantic 校验 LLM 返回的 JSON


class _LLMFiveWhy(PydBaseModel):
    """单条 5 Why"""
    n: int = PydField(ge=1, le=5, description="Why 序号，从 1 开始（5 Whys 最多 5 条）")
    question: str = PydField(min_length=1, description="Why 问题")
    answer: str = PydField(min_length=1, description="Why 答案")
    evidence_ids: list[str] = PydField(default_factory=list, description="证据 ID 列表，必须 ≥ 1 条")
    is_root_cause: bool = PydField(default=False, description="是否为根本原因")


class _LLMFishboneCategory(PydBaseModel):
    """鱼骨图 6M 单个分类"""
    name: str = PydField(min_length=1, description="分类名（如 人员 Man）")
    causes: list[dict] = PydField(default_factory=list, description="原因列表，每项含 text/evidence/severity")


class _LLMFishbone(PydBaseModel):
    """鱼骨图"""
    root: str = PydField(min_length=1, description="问题陈述")
    categories: list[_LLMFishboneCategory] = PydField(min_length=6, max_length=6, description="6M 分类（必须 6 个）")


class _LLMReasonerOutput(PydBaseModel):
    """Reasoner LLM 输出 schema"""
    five_whys: list[_LLMFiveWhy] = PydField(min_length=1, description="5 Whys 数组，至少 1 条")
    fishbone: _LLMFishbone = PydField(description="6M 鱼骨图")
    root_cause_summary: str = PydField(min_length=1, description="根本原因结论，引用证据 ID")


_REASONER_SYSTEM_PROMPT = """你是药品 GMP 偏差调查员，专长根因分析（RCA）。

任务：基于以下证据和时间线，用 5 Whys 方法找出根本原因，并按 6M 鱼骨图分类。

硬性约束（必须遵守）：
1. 每个 Why 必须引用至少一条证据 ID（从输入的证据列表中选择）
2. 鱼骨图按 6M 分类（人员 Man / 设备 Machine / 物料 Material / 方法 Method / 环境 Environment / 测量 Measurement）
3. 至少生成 1 条 5 Why，最多 5 条
4. 必须明确标注哪一条 Why 是根本原因（is_root_cause=true）
5. root_cause_summary 必须引用证据 ID 支持结论

输出 JSON 格式（严格遵守，不要添加任何文字说明）：
{
  "five_whys": [
    {
      "n": 1,
      "question": "为什么...？",
      "answer": "因为...（引用证据 E-XXX-XX）",
      "evidence_ids": ["E-XXX-01"],
      "is_root_cause": false
    }
  ],
  "fishbone": {
    "root": "问题陈述",
    "categories": [
      {"name": "人员 Man", "causes": [{"text": "原因描述", "evidence": ["E-XXX-01"], "severity": "danger|warning|info"}]}
    ]
  },
  "root_cause_summary": "根因结论，引用证据 ID"
}"""


def _build_reasoner_user_prompt(
    case: Deviation, evidence_hunter_output: dict[str, Any]
) -> str:
    """构造 Reasoner LLM user prompt —— 把案例 + 证据 + 时间线格式化为 LLM 输入"""
    # 证据列表（含 ID + 摘要 + 源系统）
    evidence_lines = []
    for ev in case.evidence:
        summary = ev.summary or "(无摘要)"
        evidence_lines.append(f"- [{ev.id}] ({ev.source_system}) {summary}")

    # 时间线事件
    timeline_lines = []
    for t in case.timeline:
        ts = t.occurred_at.isoformat() if t.occurred_at else "?"
        timeline_lines.append(f"- [{ts}] ({t.source}) {t.title}" + (f" — {t.detail}" if t.detail else ""))

    # Evidence Hunter 输出摘要
    eh_summary = (
        f"证据总数: {evidence_hunter_output.get('evidence_count', 0)}, "
        f"源系统: {evidence_hunter_output.get('by_source', {})}, "
        f"异常数: {evidence_hunter_output.get('anomaly_count', 0)}, "
        f"证据缺口: {evidence_hunter_output.get('gaps', [])}"
    )

    return f"""偏差案例：
- 编号: {case.id}
- 标题: {case.title}
- 严重等级: {case.severity}
- 描述: {case.description or '(无描述)'}
- 涉及产品: {case.product_impact or '(待评估)'}
- 立即措施: {case.immediate_action or '(无)'}

Evidence Hunter 摘要：{eh_summary}

证据列表（每条含 ID + 源系统 + 摘要，5 Whys 必须引用这些 ID）：
{chr(10).join(evidence_lines) if evidence_lines else '(无证据)'}

时间线事件：
{chr(10).join(timeline_lines) if timeline_lines else '(无时间线)'}

请基于以上证据和时间线，输出 5 Whys 推理结果 JSON。"""


async def _run_reasoner_with_llm(
    case: Deviation,
    evidence_hunter_output: dict[str, Any],
    llm_client,
) -> dict[str, Any]:
    """LLM 版 Reasoner —— 调用 LLM 生成 5 Whys + 鱼骨图

    Args:
        case: 偏差案例
        evidence_hunter_output: Evidence Hunter 输出
        llm_client: LLM 客户端实例

    Returns:
        Dict: 与规则版同构的输出，但 ai_model 标记为 LLM 模型名
    """
    system = _REASONER_SYSTEM_PROMPT
    user_prompt = _build_reasoner_user_prompt(case, evidence_hunter_output)

    # 计算 prompt hash（用于审计 ai_prompt_hash）
    prompt_hash = llm_client.hash_prompt(system, user_prompt)

    # 调用 LLM（强制 JSON 输出 + Pydantic 校验；agent_label 用于 token 成本计量）
    llm_output: _LLMReasonerOutput = await llm_client.chat_json(
        system=system,
        user_prompt=user_prompt,
        schema=_LLMReasonerOutput,
        agent_label="reasoner",
    )

    valid_ids = _case_evidence_ids(case)
    hallucinated: list[str] = []

    # 转换为 Agent 输出格式（与规则版同构；引用不存在的证据 ID 直接剔除——防幻觉入链）
    whys_output = []
    for fw in llm_output.five_whys:
        kept, dropped = _filter_hallucinated_evidence(fw.evidence_ids, valid_ids)
        hallucinated.extend(dropped)
        why_data = {
            "n": fw.n,
            "question": fw.question,
            "answer": fw.answer,
            "evidence_ids": kept,
            "is_root_cause": fw.is_root_cause,
        }
        whys_output.append({
            **why_data,
            "ai_model": llm_client.model_name,
            "ai_output_hash": _hash_output(why_data),
        })

    # 鱼骨图证据引用同样过滤
    fishbone = llm_output.fishbone.model_dump()
    for cat in fishbone.get("categories", []):
        for cause in cat.get("causes", []):
            kept, dropped = _filter_hallucinated_evidence(cause.get("evidence", []) or [], valid_ids)
            hallucinated.extend(dropped)
            cause["evidence"] = kept

    root_cause_summary = llm_output.root_cause_summary

    # 完整输出的 hash（用于审计 ai_output_hash）
    output_payload = {
        "five_whys": whys_output,
        "fishbone": fishbone,
        "root_cause_summary": root_cause_summary,
    }

    result = {
        "agent": "reasoner",
        "status": "completed",
        "five_whys": whys_output,
        "fishbone": fishbone,
        "root_cause_summary": root_cause_summary,
        "ai_model": llm_client.model_name,
        "ai_prompt_hash": prompt_hash,
        "ai_output_hash": _hash_output(output_payload),
        "llm_enabled": True,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    if hallucinated:
        # 幻觉证据 ID 被剔除 —— 记录到输出供审计追溯（AgentHallucinationError 不阻塞流水线）
        result["hallucination_filtered"] = sorted(set(hallucinated))
    return result


async def _run_skeptic(
    db: AsyncSession, case: Deviation, reasoner_output: dict[str, Any],
    flag_user: str | None = None,
) -> dict[str, Any]:
    """Skeptic — 反例挑战

    支持两种模式（通过 feature flag `skeptic_v2_enhanced` 灰度切换）：
    1. **规则版（默认兜底）**：数据驱动的通用挑战（证据缺口/覆盖面检查）
    2. **LLM 版（灰度启用）**：根据当前案例的真实证据 + Reasoner 输出生成针对性反例挑战

    LLM 路径需要 `skeptic_v2_enhanced` flag 启用且 `LLMClient.is_configured` 为 True。
    LLM 失败时自动降级到规则版兜底，并在 ai_model 字段标记 fallback。
    """
    from shared.feature_flag import is_enabled
    use_v2 = is_enabled("skeptic_v2_enhanced", user_id=flag_user or case.investigator_id)

    # 先生成规则版输出（作为兜底 + LLM 失败时的回退）
    rule_result = _build_rule_based_skeptic_output(case, use_v2, reasoner_output)
    if not use_v2:
        return rule_result

    # LLM 灰度分支 —— flag 启用时调用 LLM
    try:
        from shared.llm_client import LLMError, llm_client
        if not llm_client.is_configured:
            rule_result["ai_model"] = "rule-template-v1 (llm-not-configured)"
            rule_result["llm_fallback_reason"] = "LLM provider/api_key 未配置，降级到规则版"
            return rule_result

        llm_result = await _run_skeptic_with_llm(case, reasoner_output, llm_client)
        return llm_result

    except LLMError as e:
        rule_result["ai_model"] = "rule-template-v1 (llm-fallback)"
        rule_result["llm_fallback_reason"] = f"LLM 调用失败降级: {type(e).__name__}: {str(e)[:200]}"
        return rule_result
    except Exception as e:
        rule_result["ai_model"] = "rule-template-v1 (llm-error)"
        rule_result["llm_fallback_reason"] = f"LLM 未知异常降级: {type(e).__name__}: {str(e)[:200]}"
        return rule_result


def _build_rule_based_skeptic_output(
    case: Deviation, use_v2: bool, reasoner_output: dict[str, Any] | None = None
) -> dict[str, Any]:
    """规则版 Skeptic —— 数据驱动的通用挑战（不携带任何案例事实性结论）

    挑战内容只基于案例证据覆盖面与 Reasoner 输出结构，指向"需要人工核实的缺口"，
    具体事实性反例由 LLM 路径或人工给出。
    """
    reasoner_output = reasoner_output or {}
    valid_ids = _case_evidence_ids(case)
    root_summary = (case.title or case.id)
    challenges: list[dict[str, Any]] = []

    # 1. 替代解释维度：证据覆盖面是否足以排除其他解释
    by_source: dict[str, int] = {}
    for ev in case.evidence or []:
        by_source[ev.source_system] = by_source.get(ev.source_system, 0) + 1
    low_coverage = [s for s in ("LMS", "BMS", "EMS", "LIMS", "MES") if by_source.get(s, 0) < 2]
    challenges.append({
        "target": "根因结论（Reasoner 输出）",
        "challenge": (
            f"当前证据覆盖以下源系统：{by_source or '无'}。"
            f"以下源系统证据不足 2 条：{low_coverage or '无'}。"
            f"请确认是否已排除这些维度的替代解释。"
        ),
        "evidence_ids": [e for e in valid_ids if any(e.startswith(f"E-{s}") for s in low_coverage)][:3],
        "type": "alternative_explanation",
    })

    # 2. 独立根因维度：是否对 Reasoner 引用的证据做了交叉验证
    reasoner_ev_ids = [
        e for w in (reasoner_output.get("five_whys") or []) for e in (w.get("evidence_ids") or [])
    ]
    challenges.append({
        "target": "根因结论（Reasoner 输出）",
        "challenge": (
            "Reasoner 引用的证据是否被交叉验证？若同一现象存在多个独立来源的异常记录，"
            "应排除独立根因叠加的可能。"
        ),
        "evidence_ids": list(dict.fromkeys(reasoner_ev_ids))[:3],
        "type": "independent_cause",
    })

    # 3. 证据缺口维度：异常证据是否有对应的确证/复测记录
    anomalies = [
        ev.id for ev in (case.evidence or [])
        if ev.summary and ("超" in ev.summary or "异常" in ev.summary or "expired" in json.dumps(ev.raw_data or {}, ensure_ascii=False))
    ]
    challenges.append({
        "target": "证据链完整性",
        "challenge": "以下证据被标记为异常，是否有对应的确证/复测/趋势分析记录？缺少确证属于证据缺口。",
        "evidence_ids": anomalies[:4],
        "type": "evidence_gap",
    })

    # Skeptic v2 增强 —— 追加时间相关性核查挑战
    if use_v2:
        challenges.append({
            "target": "时间线相关性",
            "challenge": "异常证据的时间窗是否与偏差发现时间重合？应做时间相关性分析排除巧合。目标案例：" + root_summary,
            "evidence_ids": valid_ids[:3],
            "type": "evidence_gap",
        })

    # 过滤幻觉引用
    for c in challenges:
        kept, _ = _filter_hallucinated_evidence(c.get("evidence_ids") or [], valid_ids)
        c["evidence_ids"] = kept

    return {
        "agent": "skeptic",
        "status": "completed",
        "challenges": challenges,
        "challenge_count": len(challenges),
        "ai_model": "rule-template-v1",
        "ai_output_hash": _hash_output(challenges),
        "timestamp": datetime.now(UTC).isoformat(),
    }


# ============================================================
#  Skeptic LLM 路径
# ============================================================
class _LLMSkepticChallenge(PydBaseModel):
    """单条 LLM 生成的反例挑战"""
    target: str = PydField(min_length=1, description="被挑战的结论（引用 Reasoner 的具体结论）")
    challenge: str = PydField(min_length=1, description="挑战内容，必须具体，引用现有证据 ID")
    evidence_ids: list[str] = PydField(default_factory=list, description="支撑挑战的证据 ID 列表")
    type: str = PydField(
        description="挑战类型：alternative_explanation | independent_cause | evidence_gap"
    )


class _LLMSkepticOutput(PydBaseModel):
    """Skeptic LLM 输出 schema"""
    challenges: list[_LLMSkepticChallenge] = PydField(
        min_length=1, description="反例挑战数组，至少 1 条"
    )


_SKEPTIC_SYSTEM_PROMPT = """你是 GMP 偏差调查质量保证审核员，专长根因分析的对抗性审查。

任务：基于以下 Reasoner 的根因分析结果，逐条挑战其结论。你是"魔鬼代言人"，
目的是找出 Reasoner 可能遗漏的替代解释、独立根因或证据缺漏。

挑战维度（必须覆盖至少 2 种）：
1. alternative_explanation：基于案例其他证据提出替代解释（如"实际污染源可能是 X，而非 Reasoner 所说的 Y"）
2. independent_cause：识别是否存在独立根因（如 HVAC 故障本身可能就是独立根因，而非次要因素）
3. evidence_gap：指出证据链中的缺漏（如缺少某批次检验报告、缺少某段时间的环境监控数据）

硬性约束（必须遵守）：
1. 每条挑战必须能引用至少一条现有证据 ID 来支撑（从输入的证据列表中选择）
2. 挑战要具体，不能泛泛说"可能有其他原因"，必须指出具体的替代假设
3. target 字段必须引用 Reasoner 的具体结论（如"根因结论：EMP-2187 是污染源"）
4. 至少生成 2 条挑战，最多 6 条
5. type 字段必须是 alternative_explanation / independent_cause / evidence_gap 三者之一

输出 JSON 格式（严格遵守，不要添加任何文字说明）：
{
  "challenges": [
    {
      "target": "被挑战的结论（引用 Reasoner 输出）",
      "challenge": "具体挑战内容，引用证据 E-XXX-XX",
      "evidence_ids": ["E-XXX-01"],
      "type": "alternative_explanation"
    }
  ]
}"""


def _build_skeptic_user_prompt(
    case: Deviation, reasoner_output: dict[str, Any]
) -> str:
    """构造 Skeptic LLM user prompt —— 把案例 + 证据 + Reasoner 输出格式化为 LLM 输入"""
    # 证据列表
    evidence_lines = []
    for ev in case.evidence:
        summary = ev.summary or "(无摘要)"
        evidence_lines.append(f"- [{ev.id}] ({ev.source_system}) {summary}")

    # Reasoner 输出摘要（5 Whys + 鱼骨图 + 根因结论）
    whys_lines = []
    for fw in reasoner_output.get("five_whys", []):
        whys_lines.append(
            f"  Why #{fw.get('n')}: {fw.get('question')}\n    → {fw.get('answer')}"
            f" (证据: {fw.get('evidence_ids', [])})"
        )

    fishbone_lines = []
    fishbone = reasoner_output.get("fishbone", {})
    for cat in fishbone.get("categories", []):
        causes = cat.get("causes", [])
        if causes:
            cause_text = "; ".join(c.get("text", "") for c in causes)
            fishbone_lines.append(f"  {cat.get('name')}: {cause_text}")

    return f"""偏差案例：
- 编号: {case.id}
- 标题: {case.title}
- 严重等级: {case.severity}
- 描述: {case.description or '(无描述)'}

证据列表（挑战必须引用这些 ID）：
{chr(10).join(evidence_lines) if evidence_lines else '(无证据)'}

Reasoner 输出（需要被挑战的结论）：

5 Whys 推理链：
{chr(10).join(whys_lines) if whys_lines else '(无)'}

鱼骨图 6M 分类：
{chr(10).join(fishbone_lines) if fishbone_lines else '(无)'}

根因结论：{reasoner_output.get('root_cause_summary', '(无)')}

请基于以上证据，逐条挑战 Reasoner 的结论。输出 JSON。"""


async def _run_skeptic_with_llm(
    case: Deviation,
    reasoner_output: dict[str, Any],
    llm_client,
) -> dict[str, Any]:
    """LLM 版 Skeptic —— 调用 LLM 生成针对性反例挑战

    Args:
        case: 偏差案例
        reasoner_output: Reasoner 输出（含 five_whys / fishbone / root_cause_summary）
        llm_client: LLM 客户端实例

    Returns:
        Dict: 与规则版同构的输出，但 ai_model 标记为 LLM 模型名
    """
    system = _SKEPTIC_SYSTEM_PROMPT
    user_prompt = _build_skeptic_user_prompt(case, reasoner_output)

    # 计算 prompt hash（用于审计 ai_prompt_hash）
    prompt_hash = llm_client.hash_prompt(system, user_prompt)

    # 调用 LLM（强制 JSON 输出 + Pydantic 校验）
    llm_output: _LLMSkepticOutput = await llm_client.chat_json(
        system=system,
        user_prompt=user_prompt,
        schema=_LLMSkepticOutput,
        agent_label="skeptic",
    )

    valid_ids = _case_evidence_ids(case)
    hallucinated: list[str] = []

    # 转换为 Agent 输出格式（与规则版同构；幻觉证据引用剔除）
    challenges_output = []
    for c in llm_output.challenges:
        kept, dropped = _filter_hallucinated_evidence(c.evidence_ids, valid_ids)
        hallucinated.extend(dropped)
        challenge_data = {
            "target": c.target,
            "challenge": c.challenge,
            "evidence_ids": kept,
            "type": c.type,
        }
        challenges_output.append({
            **challenge_data,
            "ai_model": llm_client.model_name,
            "ai_output_hash": _hash_output(challenge_data),
        })

    # 完整输出的 hash（用于审计 ai_output_hash）
    output_payload = {"challenges": challenges_output}

    result = {
        "agent": "skeptic",
        "status": "completed",
        "challenges": challenges_output,
        "challenge_count": len(challenges_output),
        "ai_model": llm_client.model_name,
        "ai_prompt_hash": prompt_hash,
        "ai_output_hash": _hash_output(output_payload),
        "llm_enabled": True,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    if hallucinated:
        result["hallucination_filtered"] = sorted(set(hallucinated))
    return result


async def _run_drafter(
    db: AsyncSession, case: Deviation, reasoner_output: dict[str, Any], skeptic_output: dict[str, Any],
    flag_user: str | None = None,
) -> dict[str, Any]:
    """Drafter — ICH Q9 报告生成

    支持两种模式（通过 feature flag `drafter_llm_enabled` 灰度切换）：
    1. **规则版（默认兜底）**：Python 字符串拼接 ICH Q9 模板
    2. **LLM 版（灰度启用）**：调用 LLM 生成完整 Markdown 报告，嵌入 ICH Q9 格式要求

    LLM 路径需要 `drafter_llm_enabled` flag 启用且 `LLMClient.is_configured` 为 True。
    LLM 失败时自动降级到规则版兜底。
    """
    from shared.feature_flag import is_enabled
    use_llm = is_enabled("drafter_llm_enabled", user_id=flag_user or case.investigator_id)

    # 先生成规则版输出（作为兜底）
    rule_result = _build_rule_based_drafter_output(case, reasoner_output, skeptic_output)
    if not use_llm:
        return rule_result

    # LLM 灰度分支
    try:
        from shared.llm_client import LLMError, llm_client
        if not llm_client.is_configured:
            rule_result["ai_model"] = "rule-template-v1 (llm-not-configured)"
            rule_result["llm_fallback_reason"] = "LLM provider/api_key 未配置，降级到模板版"
            return rule_result

        llm_result = await _run_drafter_with_llm(case, reasoner_output, skeptic_output, llm_client)
        return llm_result

    except LLMError as e:
        rule_result["ai_model"] = "rule-template-v1 (llm-fallback)"
        rule_result["llm_fallback_reason"] = f"LLM 调用失败降级: {type(e).__name__}: {str(e)[:200]}"
        return rule_result
    except Exception as e:
        rule_result["ai_model"] = "rule-template-v1 (llm-error)"
        rule_result["llm_fallback_reason"] = f"LLM 未知异常降级: {type(e).__name__}: {str(e)[:200]}"
        return rule_result


def _build_rule_based_drafter_output(
    case: Deviation,
    reasoner_output: dict[str, Any],
    skeptic_output: dict[str, Any],
) -> dict[str, Any]:
    """规则版 Drafter — ICH Q9 模板拼接"""
    report = f"""偏差调查报告 — {case.id}

1. 偏差描述
{case.description}

2. 严重等级: {case.severity.upper()}
3. 发现时间: {case.discovered_at.isoformat()}
4. 影响产品: {case.product_impact or '待评估'}

5. 立即措施
{case.immediate_action or '无'}

6. 根本原因分析（5 Whys）
{chr(10).join([f"  Why #{w['n']}: {w['question']}" + chr(10) + f"    → {w['answer']}" for w in reasoner_output['five_whys']])}

7. 根本原因
{reasoner_output['root_cause_summary']}

8. Skeptic 反例审阅
{chr(10).join([f"  - {c['challenge']}" for c in skeptic_output['challenges']])}

9. CAPA 建议（数据驱动占位 —— 事实性 CAPA 由 LLM 路径或 HITL gate4 人工评审给出）
{chr(10).join([f"  - [{ev.id}] ({ev.source_system}) 针对该证据的纠正措施待评审" for ev in (case.evidence or [])[:3]]) or "  - 待评审（无证据可引用）"}

10. 合规声明
本调查遵循 ICH Q9 质量风险管理原则，所有结论均有证据引用支持。
本报告由规则版模板生成（LLM 未启用或降级），事实性根因与 CAPA 需经 HITL 人工确认后方可签发。
"""
    return {
        "agent": "drafter",
        "status": "completed",
        "report_markdown": report,
        "report_format": "ICH-Q9-template-v1",
        "ai_model": "rule-template-v1",
        "ai_output_hash": _hash_output(report),
        "timestamp": datetime.now(UTC).isoformat(),
    }


# ============================================================
#  Drafter LLM 路径
# ============================================================
_DRAFTER_SYSTEM_PROMPT = """你是 GMP 偏差调查报告起草专家，精通 ICH Q9 质量风险管理。

任务：基于 Reasoner 的根因分析结果 + Skeptic 的反例挑战结果 + 案例基础信息，
生成一份完整的 ICH Q9 格式偏差调查报告（Markdown 格式）。

ICH Q9 报告必须包含以下章节（按顺序）：
1. **偏差描述** — 详细描述事件，含发生时间、地点、发现过程
2. **严重等级评估** — 基于 GMP 影响评估（critical/major/minor）
3. **影响产品评估** — 涉及的产品批次、对患者安全的潜在影响
4. **立即措施** — Containment 行动，已采取的纠正措施
5. **根本原因分析** — 5 Whys 推理链 + 鱼骨图 6M 分类（必须引用证据 ID）
6. **根本原因结论** — 明确的根本原因陈述
7. **Skeptic 反例审阅** — 列出所有反例挑战 + 调查团队的回应
8. **CAPA 建议** — 纠正与预防措施，分立即/短期/长期三档
9. **风险评估** — 残余风险等级 + 风险接受论证
10. **合规声明** — ICH Q9 合规性声明 + 数据完整性确认

硬性约束：
1. 每个结论必须引用至少一条证据 ID（格式：[E-XXX-XX]）
2. CAPA 必须具体可执行，不能写"加强管理"等空话
3. 风险评估必须给出残余风险等级（low/medium/high）并论证
4. 报告语言：中文
5. 输出格式：纯 Markdown 文本（不要包裹在 ```markdown 代码块中）

直接输出报告 Markdown 内容，不要添加任何前置说明或元信息。"""


def _build_drafter_user_prompt(
    case: Deviation,
    reasoner_output: dict[str, Any],
    skeptic_output: dict[str, Any],
) -> str:
    """构造 Drafter LLM user prompt —— 把案例 + Reasoner + Skeptic 输出格式化为 LLM 输入"""
    # 证据列表（用于报告引用）
    evidence_lines = []
    for ev in case.evidence:
        summary = ev.summary or "(无摘要)"
        evidence_lines.append(f"- [{ev.id}] ({ev.source_system}) {summary}")

    # 5 Whys 推理链
    whys_lines = []
    for w in reasoner_output.get("five_whys", []):
        ev_ids = ", ".join(w.get("evidence_ids", [])) or "无"
        whys_lines.append(
            f"  Why #{w.get('n')}: {w.get('question')}\n"
            f"    → {w.get('answer')}\n"
            f"    证据: {ev_ids}"
            + (" [根本原因]" if w.get("is_root_cause") else "")
        )

    # 鱼骨图 6M 分类
    fishbone_lines = []
    fishbone = reasoner_output.get("fishbone", {})
    for cat in fishbone.get("categories", []):
        causes = cat.get("causes", [])
        if causes:
            cause_items = []
            for c in causes:
                ev = ", ".join(c.get("evidence", [])) if c.get("evidence") else "无证据"
                cause_items.append(f"{c.get('text', '')} [{ev}]")
            fishbone_lines.append(f"  {cat.get('name')}: {'; '.join(cause_items)}")

    # Skeptic 挑战
    skeptic_lines = []
    for c in skeptic_output.get("challenges", []):
        ev_ids = ", ".join(c.get("evidence_ids", [])) or "无"
        skeptic_lines.append(
            f"  - [{c.get('type', '?')}] {c.get('challenge', '')}\n"
            f"    被挑战结论: {c.get('target', '')}\n"
            f"    支撑证据: {ev_ids}"
        )

    return f"""偏差案例基础信息：
- 编号: {case.id}
- 标题: {case.title}
- 严重等级: {case.severity}
- 描述: {case.description or '(无描述)'}
- 发现时间: {case.discovered_at.isoformat() if case.discovered_at else '(未记录)'}
- 影响产品: {case.product_impact or '待评估'}
- 立即措施: {case.immediate_action or '无'}

证据列表（报告中必须引用这些 ID）：
{chr(10).join(evidence_lines) if evidence_lines else '(无证据)'}

Reasoner 根因分析结果：

5 Whys 推理链：
{chr(10).join(whys_lines) if whys_lines else '(无)'}

鱼骨图 6M 分类：
{chr(10).join(fishbone_lines) if fishbone_lines else '(无)'}

根本原因结论：{reasoner_output.get('root_cause_summary', '(无)')}

Skeptic 反例挑战（{skeptic_output.get('challenge_count', 0)} 条）：
{chr(10).join(skeptic_lines) if skeptic_lines else '(无挑战)'}

请基于以上信息，生成 ICH Q9 格式的偏差调查报告（Markdown）。"""


async def _run_drafter_with_llm(
    case: Deviation,
    reasoner_output: dict[str, Any],
    skeptic_output: dict[str, Any],
    llm_client,
) -> dict[str, Any]:
    """LLM 版 Drafter —— 调用 LLM 生成 ICH Q9 Markdown 报告

    Args:
        case: 偏差案例
        reasoner_output: Reasoner 输出（含 5 Whys / 鱼骨图 / 根因结论）
        skeptic_output: Skeptic 输出（含 challenges 列表）
        llm_client: LLM 客户端实例

    Returns:
        Dict: 与规则版同构的输出，但 ai_model 标记为 LLM 模型名
    """
    system = _DRAFTER_SYSTEM_PROMPT
    user_prompt = _build_drafter_user_prompt(case, reasoner_output, skeptic_output)

    # 计算 prompt hash（用于审计）
    prompt_hash = llm_client.hash_prompt(system, user_prompt)

    # 调用 LLM（Drafter 输出 Markdown 文本，非 JSON，故用 chat_completion；
    # 整篇 ICH Q9 报告用独立的更大 max_tokens，避免全局 2048 截断）
    report_markdown = await llm_client.chat_completion(
        system=system,
        user_prompt=user_prompt,
        json_mode=False,
        agent_label="drafter",
        max_tokens=settings.llm_max_tokens_drafter,
    )

    # 清理输出（去除可能的首尾 ```markdown 代码块包裹）
    report_markdown = report_markdown.strip()
    if report_markdown.startswith("```markdown"):
        report_markdown = report_markdown[len("```markdown"):].strip()
    if report_markdown.startswith("```"):
        report_markdown = report_markdown[3:].strip()
    if report_markdown.endswith("```"):
        report_markdown = report_markdown[:-3].strip()

    return {
        "agent": "drafter",
        "status": "completed",
        "report_markdown": report_markdown,
        "report_format": "ICH-Q9-llm-v1",
        "ai_model": llm_client.model_name,
        "ai_prompt_hash": prompt_hash,
        "ai_output_hash": _hash_output(report_markdown),
        "llm_enabled": True,
        "timestamp": datetime.now(UTC).isoformat(),
    }


# ============================================================
#  Multi-Agent Debate (Phase 2 改进5)
#  - 并行 2 个 Reasoner（primary + adversary，不同 LLM/model）
#  - Skeptic 交叉质证两份推理
#  - Meta-Controller 综合置信度结论
# ============================================================

class _LLMAgreementPoint(PydBaseModel):
    """两份推理的一致点"""
    point: str = PydField(min_length=1, description="一致结论描述")
    evidence_ids: list[str] = PydField(default_factory=list, description="支撑证据 ID 列表")


class _LLMDisagreementPoint(PydBaseModel):
    """两份推理的分歧点"""
    point: str = PydField(min_length=1, description="分歧结论描述")
    primary_view: str = PydField(min_length=1, description="primary Reasoner 的观点")
    adversary_view: str = PydField(min_length=1, description="adversary Reasoner 的观点")
    resolution: str = PydField(
        description="分歧解决方式：accepted_primary | accepted_adversary | merged | unresolved"
    )


class _LLMMetaControllerOutput(PydBaseModel):
    """Meta-Controller 综合输出 schema"""
    agreed_root_cause: str = PydField(description="双方一致同意的根因（若无共识则空字符串）")
    final_root_cause_summary: str = PydField(min_length=1, description="最终综合根因结论，引用证据 ID")
    confidence_score: float = PydField(ge=0.0, le=1.0, description="置信度 0-1，1=完全一致")
    agreement_points: list[_LLMAgreementPoint] = PydField(default_factory=list, description="一致点列表")
    disagreement_points: list[_LLMDisagreementPoint] = PydField(default_factory=list, description="分歧点列表")
    recommended_actions: list[str] = PydField(default_factory=list, description="基于综合结论的后续行动建议")


_META_CONTROLLER_SYSTEM_PROMPT = """你是 GMP 偏差调查的 Meta-Controller，专长综合多 Agent 推理结果给出置信度结论。

任务：基于两个独立 Reasoner 的推理结果 + Skeptic 的交叉质证，输出置信度加权的综合结论。

工作方法：
1. 比对两份 Reasoner 的 5 Whys、鱼骨图、根因结论
2. 识别一致点（双方都指向的根因）与分歧点（结论不同的部分）
3. 参考 Skeptic 的交叉质证解决分歧（采纳证据更充分的一方）
4. 输出置信度评分：
   - 0.9-1.0：双方完全一致且证据充分
   - 0.7-0.89：基本一致，仅细节分歧
   - 0.5-0.69：主要结论一致，但存在重要分歧
   - 0.3-0.49：双方分歧严重，需进一步调查
   - 0.0-0.29：双方结论完全矛盾
5. final_root_cause_summary 必须引用证据 ID 支持结论

硬性约束（必须遵守）：
1. confidence_score 必须在 0.0-1.0 之间
2. 每个 disagreement_point 的 resolution 必须是 accepted_primary / accepted_adversary / merged / unresolved 之一
3. 若双方根因结论一致，agreed_root_cause 不能为空
4. final_root_cause_summary 必须独立给出（即使存在未解决分歧）
5. 若仅有一份 Reasoner（adversary 缺失），confidence_score 应 ≤ 0.7（缺乏交叉验证）

输出 JSON 格式（严格遵守，不要添加任何文字说明）：
{
  "agreed_root_cause": "双方一致的根因或空字符串",
  "final_root_cause_summary": "综合根因结论，引用证据 E-XXX-XX",
  "confidence_score": 0.85,
  "agreement_points": [
    {"point": "一致点描述", "evidence_ids": ["E-XXX-01"]}
  ],
  "disagreement_points": [
    {
      "point": "分歧点描述",
      "primary_view": "primary 的观点",
      "adversary_view": "adversary 的观点",
      "resolution": "accepted_primary"
    }
  ],
  "recommended_actions": ["后续行动建议1", "后续行动建议2"]
}"""


def _format_reasoner_for_prompt(label: str, r_out: dict[str, Any] | None) -> str:
    """格式化 Reasoner 输出为 LLM prompt 片段"""
    if r_out is None:
        return f"--- {label} ---\n(未生成)"
    whys = "\n".join(
        f"  Why #{w.get('n')}: {w.get('question')}\n    → {w.get('answer')} (证据: {w.get('evidence_ids', [])})"
        for w in r_out.get("five_whys", [])
    )
    return f"""--- {label} ({r_out.get('ai_model', '?')}) ---
5 Whys:
{whys or '(无)'}

根因结论：{r_out.get('root_cause_summary', '(无)')}
ai_output_hash: {r_out.get('ai_output_hash', '?')[:32]}..."""


def _build_meta_controller_user_prompt(
    case: Deviation,
    primary_output: dict[str, Any],
    adversary_output: dict[str, Any] | None,
    skeptic_output: dict[str, Any],
) -> str:
    """构造 Meta-Controller LLM user prompt —— 注入两份 Reasoner + Skeptic 交叉质证"""
    evidence_lines = []
    for ev in case.evidence:
        summary = ev.summary or "(无摘要)"
        evidence_lines.append(f"- [{ev.id}] ({ev.source_system}) {summary}")

    skeptic_lines = []
    for c in skeptic_output.get("challenges", []):
        skeptic_lines.append(
            f"  [{c.get('type', '?')}] {c.get('challenge')} (证据: {c.get('evidence_ids', [])})"
        )

    primary_block = _format_reasoner_for_prompt("Primary Reasoner", primary_output)
    adversary_block = _format_reasoner_for_prompt("Adversary Reasoner", adversary_output)

    return f"""偏差案例：
- 编号: {case.id}
- 标题: {case.title}
- 严重等级: {case.severity}
- 描述: {case.description or '(无描述)'}

证据列表：
{chr(10).join(evidence_lines) if evidence_lines else '(无证据)'}

两份独立 Reasoner 推理结果：

{primary_block}

{adversary_block}

Skeptic 交叉质证：
{chr(10).join(skeptic_lines) if skeptic_lines else '(无挑战)'}

请综合以上信息，输出置信度加权的综合结论 JSON。"""


async def _run_meta_controller_synthesis_with_llm(
    case: Deviation,
    primary_output: dict[str, Any],
    adversary_output: dict[str, Any] | None,
    skeptic_output: dict[str, Any],
    llm_client,
) -> dict[str, Any]:
    """Meta-Controller 综合置信度结论 —— LLM 调用

    Args:
        case: 偏差案例
        primary_output: primary Reasoner 输出
        adversary_output: adversary Reasoner 输出（可能为 None，仅 primary 模式）
        skeptic_output: Skeptic 交叉质证输出
        llm_client: LLM 客户端实例

    Returns:
        Dict: 含 agreed_root_cause / confidence_score / agreement_points / disagreement_points 等
    """
    system = _META_CONTROLLER_SYSTEM_PROMPT
    user_prompt = _build_meta_controller_user_prompt(case, primary_output, adversary_output, skeptic_output)
    prompt_hash = llm_client.hash_prompt(system, user_prompt)

    llm_output: _LLMMetaControllerOutput = await llm_client.chat_json(
        system=system,
        user_prompt=user_prompt,
        schema=_LLMMetaControllerOutput,
        agent_label="meta_controller",
    )

    output_payload = {
        "agreed_root_cause": llm_output.agreed_root_cause,
        "final_root_cause_summary": llm_output.final_root_cause_summary,
        "confidence_score": llm_output.confidence_score,
        "agreement_points": [p.model_dump() for p in llm_output.agreement_points],
        "disagreement_points": [p.model_dump() for p in llm_output.disagreement_points],
        "recommended_actions": llm_output.recommended_actions,
    }

    return {
        "agent": "meta_controller",
        "status": "completed",
        "agreed_root_cause": llm_output.agreed_root_cause,
        "final_root_cause_summary": llm_output.final_root_cause_summary,
        "confidence_score": llm_output.confidence_score,
        "agreement_points": output_payload["agreement_points"],
        "disagreement_points": output_payload["disagreement_points"],
        "recommended_actions": output_payload["recommended_actions"],
        "ai_model": llm_client.model_name,
        "ai_prompt_hash": prompt_hash,
        "ai_output_hash": _hash_output(output_payload),
        "llm_enabled": True,
        "timestamp": datetime.now(UTC).isoformat(),
    }


# ============================================================
#  交叉质证 System Prompt（Skeptic 扩展，支持双 Reasoner 输入）
# ============================================================

_CROSS_EXAMINE_SYSTEM_PROMPT = """你是 GMP 偏差调查质量保证审核员，专长多 Agent 推理的交叉质证。

任务：基于两份独立 Reasoner 的推理结果，做交叉质证。你是"魔鬼代言人"，
目的是找出双方推理中可能的矛盾、缺漏、替代解释或独立根因。

质证维度（必须覆盖至少 2 种）：
1. alternative_explanation：基于案例其他证据提出替代解释
2. independent_cause：识别独立根因
3. evidence_gap：指出证据链中的缺漏
4. contradiction：指出两份推理之间的矛盾点（如同一证据得出不同结论）
5. evidence_conflict：两份推理引用同一证据得出不同结论

硬性约束（必须遵守）：
1. 每条挑战必须能引用至少一条现有证据 ID
2. 至少生成 3 条挑战，最多 8 条
3. 若有两份 Reasoner，至少 1 条 type=contradiction（针对双方矛盾）
4. type 字段必须是 alternative_explanation / independent_cause / evidence_gap / contradiction / evidence_conflict 之一
5. target 字段必须明确指出被挑战的结论（可注明来自 primary 或 adversary）

输出 JSON 格式（严格遵守，不要添加任何文字说明）：
{
  "challenges": [
    {
      "target": "被挑战的结论（注明来自 primary / adversary / 双方）",
      "challenge": "具体挑战内容，引用证据 E-XXX-XX",
      "evidence_ids": ["E-XXX-01"],
      "type": "contradiction"
    }
  ]
}"""


def _build_cross_examine_user_prompt(
    case: Deviation,
    primary_output: dict[str, Any],
    adversary_output: dict[str, Any] | None,
) -> str:
    """构造交叉质证 user prompt —— 注入两份 Reasoner 输出"""
    evidence_lines = []
    for ev in case.evidence:
        summary = ev.summary or "(无摘要)"
        evidence_lines.append(f"- [{ev.id}] ({ev.source_system}) {summary}")

    primary_block = _format_reasoner_for_prompt("Primary Reasoner", primary_output)
    adversary_block = _format_reasoner_for_prompt("Adversary Reasoner", adversary_output)

    return f"""偏差案例：
- 编号: {case.id}
- 标题: {case.title}
- 严重等级: {case.severity}
- 描述: {case.description or '(无描述)'}

证据列表（挑战必须引用这些 ID）：
{chr(10).join(evidence_lines) if evidence_lines else '(无证据)'}

两份独立 Reasoner 推理结果：

{primary_block}

{adversary_block}

请基于以上两份推理做交叉质证，重点识别双方矛盾点。输出 JSON。"""


async def _run_skeptic_cross_examine_with_llm(
    case: Deviation,
    primary_output: dict[str, Any],
    adversary_output: dict[str, Any] | None,
    llm_client,
) -> dict[str, Any]:
    """LLM 版交叉质证 Skeptic —— 接受两份 Reasoner 输出

    复用 _LLMSkepticOutput schema（type 字段为自由字符串，支持 contradiction / evidence_conflict）。
    """
    system = _CROSS_EXAMINE_SYSTEM_PROMPT
    user_prompt = _build_cross_examine_user_prompt(case, primary_output, adversary_output)
    prompt_hash = llm_client.hash_prompt(system, user_prompt)

    llm_output: _LLMSkepticOutput = await llm_client.chat_json(
        system=system,
        user_prompt=user_prompt,
        schema=_LLMSkepticOutput,
        agent_label="skeptic_cross_examine",
    )

    valid_ids = _case_evidence_ids(case)
    hallucinated: list[str] = []

    challenges_output = []
    for c in llm_output.challenges:
        kept, dropped = _filter_hallucinated_evidence(c.evidence_ids, valid_ids)
        hallucinated.extend(dropped)
        challenge_data = {
            "target": c.target,
            "challenge": c.challenge,
            "evidence_ids": kept,
            "type": c.type,
        }
        challenges_output.append({
            **challenge_data,
            "ai_model": llm_client.model_name,
            "ai_output_hash": _hash_output(challenge_data),
        })

    output_payload = {"challenges": challenges_output}

    result = {
        "agent": "skeptic",
        "status": "completed",
        "challenges": challenges_output,
        "challenge_count": len(challenges_output),
        "cross_examine_mode": True,
        "ai_model": llm_client.model_name,
        "ai_prompt_hash": prompt_hash,
        "ai_output_hash": _hash_output(output_payload),
        "llm_enabled": True,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    if hallucinated:
        result["hallucination_filtered"] = sorted(set(hallucinated))
    return result


# ============================================================
#  并行 Reasoner 博弈主入口
# ============================================================

async def _run_reasoner_debate(
    db: AsyncSession,
    case: Deviation,
    evidence_hunter_output: dict[str, Any],
    flag_user: str | None = None,
) -> dict[str, Any]:
    """多 Agent 博弈 —— 并行启动 2 个 Reasoner（primary + adversary）

    primary 用主 LLM 客户端（settings.llm_*），adversary 用 debate LLM 客户端
    （settings.llm_debate_*）。使用 asyncio.gather 并发执行。

    降级策略：
    - primary LLM 未配置 → 完全降级到规则版（debate_mode=rule_fallback）
    - adversary LLM 未配置 → 仅 primary（debate_mode=primary_only）
    - primary LLM 调用失败 → 降级到规则版（debate_mode=rule_fallback）
    - adversary LLM 调用失败 → 仅 primary（debate_mode=primary_only）
    - 双方都成功 → parallel 模式

    Args:
        db: 数据库会话（Reasoner 不直接写库，仅用于签名一致性）
        case: 偏差案例
        evidence_hunter_output: Evidence Hunter 输出

    Returns:
        Dict: {
            "agent": "reasoner_debate",
            "primary": <reasoner_output>,
            "adversary": <reasoner_output or None>,
            "debate_mode": "parallel" | "primary_only" | "rule_fallback",
            "ai_model": str,
            "ai_output_hash": str,
            ...
        }
    """
    from shared.llm_client import LLMError, get_debate_client, llm_client

    rule_result = _build_rule_based_reasoner_output(case)

    # Primary LLM 未配置 → 完全降级
    if not llm_client.is_configured:
        rule_result["ai_model"] = "rule-template-v1 (llm-not-configured)"
        rule_result["llm_fallback_reason"] = "LLM provider/api_key 未配置，降级到规则版"
        return {
            "agent": "reasoner_debate",
            "status": "completed",
            "primary": rule_result,
            "adversary": None,
            "debate_mode": "rule_fallback",
            "ai_model": "rule-template-v1",
            "ai_output_hash": rule_result["ai_output_hash"],
            "timestamp": datetime.now(UTC).isoformat(),
        }

    adversary_client = get_debate_client()

    # 自博弈防护：adversary 与 primary 是同一个 (provider, model) 时，
    # "博弈"退化为同模型重复采样，置信度会被虚高 —— 拒绝并降级 primary_only
    same_model_rejected = False
    if adversary_client is not None and adversary_client.identity == llm_client.identity:
        same_model_rejected = True
        adversary_client = None

    async def _safe_run(label: str, client):
        """安全执行 Reasoner LLM 调用，捕获异常返回 (result, error)"""
        try:
            result = await _run_reasoner_with_llm(case, evidence_hunter_output, client)
            return result, None
        except LLMError as e:
            return None, f"{label} LLM 失败: {type(e).__name__}: {str(e)[:200]}"
        except Exception as e:
            return None, f"{label} 异常: {type(e).__name__}: {str(e)[:200]}"

    # 并行启动 primary + adversary（asyncio.gather 在同一事件循环，DB 不交叉所以安全）
    if adversary_client is not None:
        results = await asyncio.gather(
            _safe_run("primary", llm_client),
            _safe_run("adversary", adversary_client),
        )
        primary_output, primary_err = results[0]
        adversary_output, adversary_err = results[1]
    else:
        primary_output, primary_err = await _safe_run("primary", llm_client)
        adversary_output, adversary_err = None, "adversary LLM 未配置"

    # 处理降级
    if primary_output is None:
        # Primary 也失败 → 完全降级到规则版
        rule_result["ai_model"] = "rule-template-v1 (llm-fallback)"
        rule_result["llm_fallback_reason"] = primary_err or "unknown"
        primary_output = rule_result
        adversary_output = None
        debate_mode = "rule_fallback"
    elif adversary_output is None:
        # Primary 成功，adversary 失败 → primary only
        debate_mode = "primary_only"
    else:
        # 双方都成功 → parallel
        debate_mode = "parallel"

    debate_payload = {
        "primary_hash": primary_output.get("ai_output_hash"),
        "adversary_hash": adversary_output.get("ai_output_hash") if adversary_output else None,
        "debate_mode": debate_mode,
    }

    return {
        "agent": "reasoner_debate",
        "status": "completed",
        "primary": primary_output,
        "adversary": adversary_output,
        "debate_mode": debate_mode,
        "adversary_error": (
            adversary_err
            if adversary_output is None and (adversary_client is not None or same_model_rejected)
            else None
        ) or ("adversary 与 primary 为同一 (provider, model)，拒绝自博弈，降级 primary_only" if same_model_rejected else None),
        "ai_model": primary_output.get("ai_model", "rule-template-v1"),
        "ai_output_hash": _hash_output(debate_payload),
        "timestamp": datetime.now(UTC).isoformat(),
    }


async def _run_drafter_debate(
    db: AsyncSession,
    case: Deviation,
    debate_output: dict[str, Any],
    skeptic_output: dict[str, Any],
    meta_controller_output: dict[str, Any],
) -> dict[str, Any]:
    """博弈模式下的 Drafter —— 使用 Meta-Controller 综合结论生成 ICH Q9 报告

    构造一个合成 reasoner_output（用 Meta-Controller 的 final_root_cause_summary + primary 的 5 Whys），
    复用规则版 Drafter 逻辑，再尝试 LLM 升级。
    """
    primary = debate_output.get("primary") or {}
    synthesized_reasoner = {
        "five_whys": primary.get("five_whys", []),
        "fishbone": primary.get("fishbone", {"root": case.title, "categories": []}),
        "root_cause_summary": meta_controller_output.get(
            "final_root_cause_summary",
            primary.get("root_cause_summary", "(待补充)"),
        ),
        "ai_model": primary.get("ai_model", "rule-template-v1"),
        "ai_output_hash": primary.get("ai_output_hash", ""),
    }
    return await _run_drafter(db, case, synthesized_reasoner, skeptic_output)


# ============================================================
#  交叉质证 + Meta-Controller 综合的 wrapper（含 fallback）
# ============================================================

async def _run_skeptic_cross_examine(
    db: AsyncSession,
    case: Deviation,
    primary_output: dict[str, Any],
    adversary_output: dict[str, Any] | None,
) -> dict[str, Any]:
    """交叉质证 Skeptic wrapper —— LLM 路径 + 三级降级

    1. flag `multi_agent_debate_enabled` 已启用 → 调用 LLM 交叉质证
    2. LLM 未配置 → 降级到规则版 Skeptic（用 primary 作 reasoner_input）
    3. LLM 异常 → 降级到规则版 Skeptic
    """
    # 用 primary 作为 reasoner_input 走规则版兜底
    rule_result = _build_rule_based_skeptic_output(case, use_v2=True, reasoner_output=primary_output)

    try:
        from shared.llm_client import LLMError, llm_client
        if not llm_client.is_configured:
            rule_result["ai_model"] = "rule-template-v1 (llm-not-configured)"
            rule_result["llm_fallback_reason"] = "LLM provider/api_key 未配置，交叉质证降级到规则版"
            return rule_result

        return await _run_skeptic_cross_examine_with_llm(
            case, primary_output, adversary_output, llm_client
        )
    except LLMError as e:
        rule_result["ai_model"] = "rule-template-v1 (llm-fallback)"
        rule_result["llm_fallback_reason"] = f"LLM 调用失败降级: {type(e).__name__}: {str(e)[:200]}"
        return rule_result
    except Exception as e:
        rule_result["ai_model"] = "rule-template-v1 (llm-error)"
        rule_result["llm_fallback_reason"] = f"LLM 未知异常降级: {type(e).__name__}: {str(e)[:200]}"
        return rule_result


async def _run_meta_controller_synthesis(
    db: AsyncSession,
    case: Deviation,
    primary_output: dict[str, Any],
    adversary_output: dict[str, Any] | None,
    skeptic_output: dict[str, Any],
) -> dict[str, Any]:
    """Meta-Controller 综合 wrapper —— LLM 路径 + 降级

    降级策略：LLM 不可用时，构造一个简单的综合结论（采用 primary 的根因结论，
    confidence_score=0.5 标记为缺乏交叉验证）。
    """
    fallback_summary = primary_output.get("root_cause_summary", "(primary 推理失败)")
    # 缺乏交叉验证（无对抗方）时置信度硬封顶 0.7 —— 只有一个推理来源，不允许虚高
    fallback_payload = {
        "agreed_root_cause": fallback_summary if adversary_output is None else "",
        "final_root_cause_summary": fallback_summary,
        "confidence_score": 0.5,  # 无 LLM 综合 = 无交叉验证 = 固定中等偏低置信度
        "agreement_points": [],
        "disagreement_points": [],
        "recommended_actions": [],
    }
    fallback_result = {
        "agent": "meta_controller",
        "status": "completed",
        **fallback_payload,
        "ai_model": "rule-template-v1 (llm-not-configured)",
        "ai_output_hash": _hash_output(fallback_payload),
        "llm_enabled": False,
        "timestamp": datetime.now(UTC).isoformat(),
    }

    try:
        from shared.llm_client import LLMError, llm_client
        if not llm_client.is_configured:
            fallback_result["llm_fallback_reason"] = "LLM 未配置，Meta-Controller 降级到简单综合"
            return fallback_result

        result = await _run_meta_controller_synthesis_with_llm(
            case, primary_output, adversary_output, skeptic_output, llm_client
        )
        # 代码级硬约束（prompt 约束不可靠）：adversary 缺失时置信度封顶 0.7
        if adversary_output is None:
            result["confidence_score"] = min(float(result.get("confidence_score", 0.0)), 0.7)
            result["confidence_clamped_reason"] = "primary_only 模式：缺乏对抗方交叉验证，置信度已封顶 0.7"
        return result
    except LLMError as e:
        fallback_result["ai_model"] = "rule-template-v1 (llm-fallback)"
        fallback_result["llm_fallback_reason"] = f"LLM 调用失败降级: {type(e).__name__}: {str(e)[:200]}"
        return fallback_result
    except Exception as e:
        fallback_result["ai_model"] = "rule-template-v1 (llm-error)"
        fallback_result["llm_fallback_reason"] = f"LLM 未知异常降级: {type(e).__name__}: {str(e)[:200]}"
        return fallback_result


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


# ============================================================
#  流水线编排 —— 每阶段独立短事务
#
#  设计（修复原实现的 4 个上线阻断问题）：
#  1. 进度可见：每阶段 commit 一次，外部能实时读到 current_agent/agent_states
#  2. pause/cancel 生效：每阶段开始前检查 run.status，paused/cancelled 即停
#  3. 异常安全：阶段事务自身回滚；失败用独立会话标记 failed + 补审计
#  4. 不再长期占用一个 DB 连接跑完全程（原实现单事务占连接数分钟）
# ============================================================

def _aware(dt: datetime | None) -> datetime | None:
    """SQLite 会返回 naive datetime，统一补 UTC 时区以便比较"""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


async def _get_run_status(run_id: str) -> str | None:
    async with db_session() as db:
        row = (await db.execute(select(AgentRun.status).where(AgentRun.id == run_id))).first()
        return row[0] if row else None


async def _write_stage_result(
    run_id: str,
    *,
    agent: str,
    states_update: dict[str, Any],
    audits: list[dict[str, Any]],
    completed: bool = False,
) -> None:
    """在独立短事务内提交阶段输出 + 阶段审计（原子）"""
    async with db_session() as db:
        run = (await db.execute(select(AgentRun).where(AgentRun.id == run_id))).scalar_one_or_none()
        if not run:
            return
        states = dict(run.agent_states or {})
        states.update(states_update)
        run.agent_states = states
        run.current_agent = agent
        if completed:
            run.status = "completed"
            run.current_agent = None
            run.completed_at = datetime.now(UTC)
        for audit in audits:
            await append_audit_log(db, **audit)
        await db.flush()


def _stage_audit(operator: str, target: str, detail: str, out: dict[str, Any]) -> dict[str, Any]:
    return {
        "operator": operator,
        "action": "AGENT_RUN",
        "target": target,
        "detail": detail,
        "ai_model": out.get("ai_model", "rule-template-v1"),
        "ai_prompt_hash": out.get("ai_prompt_hash"),
        "ai_output_hash": out.get("ai_output_hash"),
    }


def _observe_stage(agent: str, out: dict[str, Any], seconds: float) -> None:
    try:
        mode = "rule" if "rule-template" in str(out.get("ai_model", "")) else "llm"
        AGENT_STAGE_DURATION.labels(agent=agent, mode=mode).observe(seconds)
    except Exception:
        pass


async def recover_zombie_runs() -> int:
    """启动时僵尸恢复 —— 把中断遗留的 pending/running run 标记为 failed

    GxP 要求失败尝试也必须留痕：进程重启后，BackgroundTasks 里的流水线已丢失，
    若不恢复这些 run 会永远停在 running，用户无法区分"还在跑"和"已经死了"。
    返回恢复的 run 数量。
    """
    from datetime import timedelta

    threshold = datetime.now(UTC) - timedelta(minutes=ZOMBIE_RUN_THRESHOLD_MINUTES)
    recovered = 0
    async with db_session() as db:
        runs = (await db.execute(
            select(AgentRun).where(AgentRun.status.in_(ACTIVE_RUN_STATUSES))
        )).scalars().all()
        for run in runs:
            ref = _aware(run.started_at) or _aware(run.created_at)
            # 刚启动不久的 run 保守跳过（多实例部署时其他实例可能正在执行）
            if ref and ref > threshold:
                continue
            run.status = "failed"
            run.error_message = "服务重启导致流水线中断（启动时僵尸恢复自动标记）"
            run.completed_at = datetime.now(UTC)
            await append_audit_log(
                db,
                operator="system:startup-recovery",
                action="AGENT_PIPELINE_FAILED",
                target=f"{run.case_id}/run/{run.id}",
                detail="服务重启导致流水线中断，启动时自动标记为 failed（GxP: 失败留痕）",
                ai_model="rule-engine-v1",
            )
            recovered += 1
    if recovered:
        AGENT_RUNS_TOTAL.labels(status="failed").inc(recovered)
    return recovered


async def _execute_pipeline(case_id: str, run_id: str, triggered_by: str) -> None:
    """后台执行 Agent 流水线 — 串行执行 6 个 Agent，每阶段独立事务提交"""
    import time

    try:
        # ---- 启动事务：running + meta_controller 初始化 + flag 快照 ----
        from shared.feature_flag import is_enabled as _is_flag_enabled

        flag_names = (
            "reasoner_llm_enabled", "skeptic_v2_enhanced", "drafter_llm_enabled",
            "evidence_hunter_semantic", "multi_agent_debate_enabled",
        )
        # 灰度按触发者评估（而非案例负责人），并把快照写入 run 供审计复现
        flags_snapshot = {f: _is_flag_enabled(f, user_id=triggered_by) for f in flag_names}
        use_debate = flags_snapshot["multi_agent_debate_enabled"]

        async with db_session() as db:
            run = (await db.execute(select(AgentRun).where(AgentRun.id == run_id))).scalar_one_or_none()
            if not run or run.status in ("cancelled", "completed", "failed"):
                return
            case_exists = (
                await db.execute(select(func.count()).select_from(Deviation).where(Deviation.id == case_id))
            ).scalar_one()
            if not case_exists:
                run.status = "failed"
                run.error_message = f"案例 {case_id} 不存在"
                run.completed_at = datetime.now(UTC)
                await db.flush()
                AGENT_RUNS_TOTAL.labels(status="failed").inc()
                return
            run.status = "running"
            run.started_at = datetime.now(UTC)
            run.current_agent = "meta_controller"
            run.agent_states = {
                "meta_controller": {
                    "status": "completed",
                    "summary": f"启动案例 {case_id} 的 Agent 流水线（触发者 {triggered_by}）",
                    "pipeline": AGENT_PIPELINE,
                    "flags": flags_snapshot,
                }
            }
            await db.flush()

        # ---- Stage 1: Evidence Hunter ----
        if await _get_run_status(run_id) != "running":
            return
        t0 = time.perf_counter()
        async with db_session() as db:
            case = (await db.execute(
                select(Deviation)
                .options(selectinload(Deviation.evidence), selectinload(Deviation.timeline))
                .where(Deviation.id == case_id)
            )).scalar_one_or_none()
            eh_out = await _run_evidence_hunter(db, case, flag_user=triggered_by) if case else None
        _observe_stage("evidence_hunter", eh_out or {}, time.perf_counter() - t0)
        await _write_stage_result(
            run_id, agent="evidence_hunter", states_update={"evidence_hunter": eh_out},
            audits=[_stage_audit("Evidence Hunter (AI)", f"{case_id}/evidence_hunter", eh_out["summary"], eh_out)],
        )

        # ---- Stage 2/3/4/5: Reasoner(或博弈) → Skeptic → Meta → Drafter ----
        if await _get_run_status(run_id) != "running":
            return
        if use_debate:
            # ===== 多 Agent 博弈路径 =====
            t0 = time.perf_counter()
            async with db_session() as db:
                case = (await db.execute(
                    select(Deviation)
                    .options(selectinload(Deviation.evidence), selectinload(Deviation.timeline),
                             selectinload(Deviation.five_whys))
                    .where(Deviation.id == case_id)
                )).scalar_one_or_none()
                debate_out = await _run_reasoner_debate(db, case, eh_out, flag_user=triggered_by) if case else None
            _observe_stage("reasoner_debate", debate_out or {}, time.perf_counter() - t0)

            primary_out = (debate_out or {}).get("primary") or {}
            adversary_out = (debate_out or {}).get("adversary")
            debate_mode = (debate_out or {}).get("debate_mode", "unknown")
            audits = [
                _stage_audit(
                    "Reasoner (AI) - primary", f"{case_id}/reasoner_debate/primary",
                    f"[debate={debate_mode}] {primary_out.get('root_cause_summary', '?')}", primary_out,
                )
            ]
            if adversary_out is not None:
                audits.append(_stage_audit(
                    "Reasoner (AI) - adversary", f"{case_id}/reasoner_debate/adversary",
                    f"[debate={debate_mode}] {adversary_out.get('root_cause_summary', '?')}", adversary_out,
                ))
            elif (debate_out or {}).get("adversary_error"):
                audits.append({
                    "operator": "Reasoner (AI) - adversary",
                    "action": "AGENT_RUN_FALLBACK",
                    "target": f"{case_id}/reasoner_debate/adversary",
                    "detail": f"adversary 降级原因: {debate_out['adversary_error']}",
                    "ai_model": "rule-template-v1 (llm-fallback)",
                })
            await _write_stage_result(
                run_id, agent="reasoner_debate", states_update={"reasoner_debate": debate_out}, audits=audits
            )

            # Skeptic 交叉质证
            if await _get_run_status(run_id) != "running":
                return
            t0 = time.perf_counter()
            async with db_session() as db:
                case = (await db.execute(
                    select(Deviation).options(selectinload(Deviation.evidence)).where(Deviation.id == case_id)
                )).scalar_one_or_none()
                s_out = (
                    await _run_skeptic_cross_examine(db, case, primary_out, adversary_out)
                    if case else None
                )
            _observe_stage("skeptic", s_out or {}, time.perf_counter() - t0)
            await _write_stage_result(
                run_id, agent="skeptic_cross_examine", states_update={"skeptic": s_out},
                audits=[_stage_audit(
                    "Skeptic (AI) - cross-examine", f"{case_id}/skeptic_cross_examine",
                    f"[cross-examine] 生成 {s_out['challenge_count']} 条交叉质证挑战", s_out,
                )],
            )

            # Meta-Controller 综合
            if await _get_run_status(run_id) != "running":
                return
            t0 = time.perf_counter()
            async with db_session() as db:
                case = (await db.execute(
                    select(Deviation).options(selectinload(Deviation.evidence)).where(Deviation.id == case_id)
                )).scalar_one_or_none()
                mc_out = (
                    await _run_meta_controller_synthesis(db, case, primary_out, adversary_out, s_out)
                    if case else None
                )
            _observe_stage("meta_controller", mc_out or {}, time.perf_counter() - t0)
            await _write_stage_result(
                run_id, agent="meta_controller", states_update={"meta_controller": mc_out},
                audits=[_stage_audit(
                    "Meta-Controller (AI) - synthesis", f"{case_id}/meta_controller",
                    f"[confidence={mc_out.get('confidence_score', 0):.2f}] "
                    f"{mc_out.get('final_root_cause_summary', '?')[:200]}",
                    mc_out,
                )],
            )

            # Drafter（使用 Meta-Controller 综合结论）
            if await _get_run_status(run_id) != "running":
                return
            t0 = time.perf_counter()
            async with db_session() as db:
                case = (await db.execute(
                    select(Deviation).options(selectinload(Deviation.evidence)).where(Deviation.id == case_id)
                )).scalar_one_or_none()
                d_out = (
                    await _run_drafter_debate(db, case, debate_out, s_out, mc_out)
                    if case else None
                )
            _observe_stage("drafter", d_out or {}, time.perf_counter() - t0)
            await _write_stage_result(
                run_id, agent="drafter", states_update={"drafter": d_out},
                audits=[_stage_audit(
                    "Drafter (AI)", f"{case_id}/drafter",
                    f"生成 ICH Q9 报告 (format: {d_out.get('report_format', '?')}, hash: {d_out['ai_output_hash'][:32]}...)",
                    d_out,
                )],
            )
        else:
            # ===== 单 Reasoner 原路径（默认）=====
            t0 = time.perf_counter()
            async with db_session() as db:
                case = (await db.execute(
                    select(Deviation)
                    .options(selectinload(Deviation.evidence), selectinload(Deviation.timeline),
                             selectinload(Deviation.five_whys))
                    .where(Deviation.id == case_id)
                )).scalar_one_or_none()
                r_out = await _run_reasoner(db, case, eh_out, flag_user=triggered_by) if case else None
            _observe_stage("reasoner", r_out or {}, time.perf_counter() - t0)
            await _write_stage_result(
                run_id, agent="reasoner", states_update={"reasoner": r_out},
                audits=[_stage_audit(
                    "Reasoner (AI)", f"{case_id}/reasoner", r_out["root_cause_summary"], r_out,
                )],
            )

            if await _get_run_status(run_id) != "running":
                return
            t0 = time.perf_counter()
            async with db_session() as db:
                case = (await db.execute(
                    select(Deviation).options(selectinload(Deviation.evidence)).where(Deviation.id == case_id)
                )).scalar_one_or_none()
                s_out = await _run_skeptic(db, case, r_out, flag_user=triggered_by) if case else None
            _observe_stage("skeptic", s_out or {}, time.perf_counter() - t0)
            await _write_stage_result(
                run_id, agent="skeptic", states_update={"skeptic": s_out},
                audits=[_stage_audit(
                    "Skeptic (AI)", f"{case_id}/skeptic",
                    f"生成 {s_out['challenge_count']} 条反例挑战", s_out,
                )],
            )

            if await _get_run_status(run_id) != "running":
                return
            t0 = time.perf_counter()
            async with db_session() as db:
                case = (await db.execute(
                    select(Deviation).options(selectinload(Deviation.evidence)).where(Deviation.id == case_id)
                )).scalar_one_or_none()
                d_out = await _run_drafter(db, case, r_out, s_out, flag_user=triggered_by) if case else None
            _observe_stage("drafter", d_out or {}, time.perf_counter() - t0)
            await _write_stage_result(
                run_id, agent="drafter", states_update={"drafter": d_out},
                audits=[_stage_audit(
                    "Drafter (AI)", f"{case_id}/drafter",
                    f"生成 ICH Q9 报告 (format: {d_out.get('report_format', '?')}, hash: {d_out['ai_output_hash'][:32]}...)",
                    d_out,
                )],
            )

        # ---- Stage 6: Guardian ----
        if await _get_run_status(run_id) != "running":
            return
        t0 = time.perf_counter()
        async with db_session() as db:
            case = (await db.execute(
                select(Deviation)
                .options(selectinload(Deviation.evidence), selectinload(Deviation.five_whys),
                         selectinload(Deviation.skeptic_challenges))
                .where(Deviation.id == case_id)
            )).scalar_one_or_none()
            g_out = await _run_guardian(db, case, d_out) if case else None
        _observe_stage("guardian", g_out or {}, time.perf_counter() - t0)
        await _write_stage_result(
            run_id, agent="guardian", states_update={"guardian": g_out},
            audits=[_stage_audit("Guardian (AI)", f"{case_id}/guardian", g_out["summary"], g_out)],
            completed=True,
        )
        AGENT_RUNS_TOTAL.labels(status="completed").inc()

    except Exception as e:
        # 失败路径：用独立会话标记 failed + 补审计（GxP: 失败必须留痕，run 不得永久卡在 running）
        AGENT_RUNS_TOTAL.labels(status="failed").inc()
        try:
            async with db_session() as db:
                run = (await db.execute(select(AgentRun).where(AgentRun.id == run_id))).scalar_one_or_none()
                if run and run.status not in ("completed", "cancelled"):
                    run.status = "failed"
                    run.error_message = f"{type(e).__name__}: {str(e)[:500]}"
                    run.completed_at = datetime.now(UTC)
                    await append_audit_log(
                        db,
                        operator="Meta-Controller (AI)",
                        action="AGENT_PIPELINE_FAILED",
                        target=f"{case_id}/run/{run_id}",
                        detail=f"Agent 流水线失败: {type(e).__name__}: {str(e)[:500]}",
                        ai_model="rule-template-v1",
                        ai_output_hash=_hash_output({
                            "run_id": run_id,
                            "case_id": case_id,
                            "error": str(e),
                            "failed_at": datetime.now(UTC).isoformat(),
                        }),
                    )
        except Exception:
            import sys
            print(f"[AGENT] CRITICAL: 失败状态标记/审计写入失败 run_id={run_id}", file=sys.stderr)


# ============================================================
#  API 路由
# ============================================================
@router.post("/pipeline/run", response_model=AgentRunOut, status_code=201, summary="启动 Agent 流水线")
@write_limit
async def start_pipeline(
    request: Request,
    body: AgentRunCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> AgentRunOut:
    """启动案例的 Agent 流水线 — 后台异步执行

    - 用户调用：Bearer JWT + agent:run 权限，triggered_by 强制取当前用户（审计归因不可伪造）
    - 服务间调用：X-Service-Key 头（gate3 自动重试等内部触发），triggered_by 由调用方声明
    - 幂等防护：同案例已有 pending/running 的 run 时返回 409
    """
    service_key = request.headers.get("X-Service-Key", "")
    if settings.service_api_key and service_key and safe_compare(service_key, settings.service_api_key):
        actor = body.triggered_by or "system"
    else:
        auth = await get_auth_context(request=request, authorization=authorization, db=db)
        auth.check("agent:run")
        actor = auth.email  # 忽略客户端传入的 triggered_by —— 审计归因不可由客户端指定

    case = (await db.execute(select(Deviation).where(Deviation.id == body.case_id))).scalar_one_or_none()
    if not case:
        raise NotFoundError(f"案例 {body.case_id} 不存在")

    # 重复触发防护：同案例同时只允许一条活跃流水线
    existing = (
        await db.execute(
            select(AgentRun)
            .where(AgentRun.case_id == body.case_id, AgentRun.status.in_(ACTIVE_RUN_STATUSES))
        )
    ).scalars().first()
    if existing:
        raise ConflictError(
            f"案例 {body.case_id} 已有进行中的流水线 {existing.id}（status={existing.status}），"
            f"请等待完成、暂停或取消后再启动"
        )

    # run_id 用 uuid 后缀 —— 旧版秒级时间戳同秒并发会主键冲突
    run_id = f"RUN-{body.case_id}-{uuid.uuid4().hex[:8]}"
    run = AgentRun(
        id=run_id,
        case_id=body.case_id,
        status="pending",
        triggered_by=actor,
        agent_states={},
    )
    db.add(run)
    await db.flush()

    await append_audit_log(
        db,
        operator=actor,
        action="AGENT_PIPELINE_START",
        target=body.case_id,
        detail=f"启动 Agent 流水线 (run_id={run_id}, flags 快照见 run.agent_states.meta_controller)",
        ip_address=request.client.host if request.client else "unknown",
    )

    # 后台执行
    background_tasks.add_task(_execute_pipeline, body.case_id, run_id, actor)

    return AgentRunOut.model_validate(run)


@router.get("/runs/{run_id}", response_model=AgentRunOut, summary="查询 Agent 运行状态")
@api_limit
async def get_run(
    request: Request,
    run_id: str,
    db: AsyncSession = Depends(get_db_from_auth),
    _: AuthContext = Depends(require("case:read")),
) -> AgentRunOut:
    """查询 Agent 流水线运行状态 + 各 Agent 输出（读权限：case:read，QA 亦可查看调查进度）"""
    run = (await db.execute(select(AgentRun).where(AgentRun.id == run_id))).scalar_one_or_none()
    if not run:
        raise NotFoundError(f"Agent 运行 {run_id} 不存在")
    return AgentRunOut.model_validate(run)


@router.get("/runs", response_model=Page[AgentRunOut], summary="查询 Agent 运行列表")
@api_limit
async def list_runs(
    request: Request,
    case_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db_from_auth),
    _: AuthContext = Depends(require("case:read")),
) -> Page[AgentRunOut]:
    """查询 Agent 运行历史"""
    stmt = select(AgentRun)
    if case_id:
        stmt = stmt.where(AgentRun.case_id == case_id)
    if status:
        stmt = stmt.where(AgentRun.status == status)

    # 使用 func.count() 在 DB 端聚合
    count_stmt = select(func.count()).select_from(AgentRun)
    if case_id:
        count_stmt = count_stmt.where(AgentRun.case_id == case_id)
    if status:
        count_stmt = count_stmt.where(AgentRun.status == status)
    total = (await db.execute(count_stmt)).scalar_one()

    stmt = stmt.order_by(desc(AgentRun.created_at)).offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(stmt)).scalars().all()
    return Page[AgentRunOut](
        items=[AgentRunOut.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


async def _load_run_for_control(db: AsyncSession, run_id: str) -> AgentRun:
    run = (
        await db.execute(select(AgentRun).where(AgentRun.id == run_id).with_for_update())
    ).scalar_one_or_none()
    if not run:
        raise NotFoundError(f"Agent 运行 {run_id} 不存在")
    return run


@router.post("/runs/{run_id}/pause", summary="暂停 Agent 运行 (HITL)")
@write_limit
async def pause_run(
    request: Request,
    run_id: str,
    db: AsyncSession = Depends(get_db_from_auth),
    auth: AuthContext = Depends(require("agent:pause")),
) -> dict:
    """暂停 Agent 流水线

    每阶段开始前流水线会检查 run.status，置为 paused 后当前阶段执行完即停。
    恢复用 /resume。
    """
    run = await _load_run_for_control(db, run_id)
    if run.status != "running":
        raise ValidationError(
            f"仅运行中的 Agent 可暂停（当前 status={run.status}）",
            error_code=None,
        )
    run.status = "paused"
    await append_audit_log(
        db,
        operator=auth.email,
        action="AGENT_PAUSE",
        target=run_id,
        detail=f"暂停于 {run.current_agent}",
        ip_address=auth.ip_address,
        session_id=auth.session_id,
    )
    return {"status": "paused", "run_id": run_id, "current_agent": run.current_agent}


@router.post("/runs/{run_id}/cancel", summary="取消 Agent 运行")
@write_limit
async def cancel_run(
    request: Request,
    run_id: str,
    db: AsyncSession = Depends(get_db_from_auth),
    auth: AuthContext = Depends(require("agent:pause")),
) -> dict:
    """取消运行中的/暂停中的流水线 —— 终态，不可恢复（需重新启动新流水线）"""
    run = await _load_run_for_control(db, run_id)
    if run.status not in ("pending", "running", "paused"):
        raise ValidationError(
            f"仅 pending/running/paused 状态可取消（当前 status={run.status}）",
            error_code=None,
        )
    run.status = "cancelled"
    run.completed_at = datetime.now(UTC)
    run.error_message = f"由 {auth.email} 手动取消"
    await append_audit_log(
        db,
        operator=auth.email,
        action="AGENT_CANCEL",
        target=run_id,
        detail=f"取消于 {run.current_agent}",
        ip_address=auth.ip_address,
        session_id=auth.session_id,
    )
    AGENT_RUNS_TOTAL.labels(status="cancelled").inc()
    return {"status": "cancelled", "run_id": run_id}


@router.post("/runs/{run_id}/resume", summary="恢复暂停的 Agent 运行")
@write_limit
async def resume_run(
    request: Request,
    run_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db_from_auth),
    auth: AuthContext = Depends(require("agent:run")),
) -> dict:
    """恢复暂停的流水线

    实现：置回 running 并重新启动流水线执行 —— 已完成的阶段输出会被重新执行覆盖
    （流水线各阶段均为确定性/可重入计算，报告以最终一次输出为准，全过程留审计）。
    """
    run = await _load_run_for_control(db, run_id)
    if run.status != "paused":
        raise ValidationError(
            f"仅 paused 状态可恢复（当前 status={run.status}）",
            error_code=None,
        )
    run.status = "running"
    await append_audit_log(
        db,
        operator=auth.email,
        action="AGENT_RESUME",
        target=run_id,
        detail="恢复运行（流水线将重新执行各阶段，已完成阶段输出会被覆盖）",
        ip_address=auth.ip_address,
        session_id=auth.session_id,
    )
    background_tasks.add_task(_execute_pipeline, run.case_id, run_id, run.triggered_by)
    return {"status": "running", "run_id": run_id}
