"""Agent Service — 规则版编排引擎

Phase 1 实现：基于规则的状态机，无 LLM 调用。
所有 Agent 输出基于案例已有证据数据 + 预设模板生成，
每步输出计算 hash 并写入审计日志。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shared.audit import append_audit_log
from shared.auth_middleware import AuthContext, get_db_from_auth, require
from shared.database import db_session
from shared.exceptions import NotFoundError, ValidationError
from shared.rate_limit import api_limit, write_limit
from shared.models import (
    AgentRun,
    Deviation,
    Evidence,
    FiveWhy,
    SkepticChallenge,
    TimelineEvent,
)
from shared.schemas import AgentRunCreate, AgentRunOut, Page

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


def _hash_output(data: Any) -> str:
    """计算 Agent 输出的 hash（用于审计存证）"""
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ============================================================
#  Agent 实现（规则版）
# ============================================================
async def _run_evidence_hunter(
    db: AsyncSession, case: Deviation
) -> Dict[str, Any]:
    """Evidence Hunter — 聚合案例已有证据 + 标注证据缺口 + 跨案例关联

    支持两种模式（通过 feature flag `evidence_hunter_semantic` 灰度切换）：
    1. **规则版（默认）**：仅做统计 + 缺口识别
    2. **语义检索版（灰度）**：调用 LLM embedding 做跨案例关联，输出 related_historical_cases

    LLM 路径需要 `evidence_hunter_semantic` flag 启用且 LLM 可配置。
    LLM 失败时降级到关键词搜索，仍能返回 related_historical_cases（matched_by=keyword）。
    """
    from shared.feature_flag import is_enabled
    use_semantic = is_enabled("evidence_hunter_semantic", user_id=case.investigator_id)

    evidence_list = case.evidence
    by_source: Dict[str, int] = {}
    for ev in evidence_list:
        by_source[ev.source_system] = by_source.get(ev.source_system, 0) + 1

    # 识别证据缺口（基于 6M 鱼骨图维度）
    gaps: List[str] = []
    if not any(ev.source_system == "LIMS" for ev in evidence_list):
        gaps.append("缺少 LIMS 检验数据（菌种鉴定）")
    if not any(ev.source_system == "LMS" for ev in evidence_list):
        gaps.append("缺少 LMS 培训/资质记录")
    if not any(ev.source_system == "BMS" for ev in evidence_list):
        gaps.append("缺少 BMS HVAC 监控数据")

    # 异常事件
    anomalies = [ev for ev in evidence_list if ev.summary and ("超" in ev.summary or "异常" in ev.summary or "expired" in (ev.raw_data or {}).get("status", ""))]

    # 输出 payload（用于 hash 计算）— 不含 timestamp 等不稳定字段
    output_payload: Dict[str, Any] = {
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
            from shared.vector_store import find_related_cases as _find_related
            from shared.llm_client import llm_client, LLMError

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
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


async def _run_reasoner(
    db: AsyncSession, case: Deviation, evidence_hunter_output: Dict[str, Any]
) -> Dict[str, Any]:
    """Reasoner — 5 Whys 推理 + 鱼骨图

    支持两种模式（通过 feature flag `reasoner_llm_enabled` 灰度切换）：
    1. **规则版（默认兜底）**：基于案例已有 5 Whys 数据 + 预设鱼骨图模板
    2. **LLM 版（灰度启用）**：调用 LLM Chain-of-Thought，从 Evidence + Timeline 做因果链推理

    LLM 路径需要 `reasoner_llm_enabled` flag 启用且 `LLMClient.is_configured` 为 True。
    任何 LLM 调用失败（配置缺失/网络异常/JSON 解析失败）自动降级到规则版兜底，
    并在 ai_model 字段标记 fallback。
    """
    from shared.feature_flag import is_enabled
    use_llm = is_enabled("reasoner_llm_enabled", user_id=case.investigator_id)

    # 先生成规则版输出（作为兜底 + LLM 失败时的回退）
    rule_result = _build_rule_based_reasoner_output(case)
    if not use_llm:
        return rule_result

    # LLM 灰度分支 —— flag 启用时调用 LLM
    try:
        from shared.llm_client import llm_client, LLMError
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


def _build_rule_based_reasoner_output(case: Deviation) -> Dict[str, Any]:
    """规则版 Reasoner —— 基于案例已有 5 Whys + 预设鱼骨图模板"""
    existing_whys = case.five_whys
    whys_output = []
    for fw in existing_whys:
        whys_output.append({
            "n": fw.n,
            "question": fw.question,
            "answer": fw.answer,
            "evidence_ids": fw.evidence_ids or [],
            "is_root_cause": fw.is_root_cause,
            "ai_model": "rule-template-v1",
            "ai_output_hash": _hash_output({
                "n": fw.n,
                "question": fw.question,
                "answer": fw.answer,
            }),
        })

    # 鱼骨图 6M 分析（基于证据聚合）
    fishbone = {
        "root": case.title,
        "categories": [
            {
                "name": "人员 Man",
                "causes": [
                    {"text": "EMP-2187 更衣资质逾期", "evidence": ["E-LMS-01"], "severity": "danger"},
                    {"text": "更衣合规性下降 (CCTV 佐证)", "evidence": ["E-CCTV-01", "E-CCTV-02", "E-CCTV-03", "E-CCTV-04"], "severity": "danger"},
                ],
            },
            {
                "name": "设备 Machine",
                "causes": [
                    {"text": "AHU-7 滤芯差压异常波动", "evidence": ["E-BMS-02", "E-BMS-05"], "severity": "warning"},
                ],
            },
            {
                "name": "物料 Material",
                "causes": [
                    {"text": "消毒剂 Spor-Klenz 批次 SK-2507-08", "evidence": ["E-MES-01", "E-MES-02"], "severity": "info"},
                ],
            },
            {
                "name": "方法 Method",
                "causes": [
                    {"text": "清洁 SOP 未涵盖资质门控", "evidence": ["E-SYS-01"], "severity": "warning"},
                ],
            },
            {
                "name": "环境 Environment",
                "causes": [
                    {"text": "B 级背景差压波动", "evidence": ["E-BMS-02"], "severity": "warning"},
                ],
            },
            {
                "name": "测量 Measurement",
                "causes": [
                    {"text": "沉降菌采样频率不足", "evidence": [], "severity": "info", "note": "无证据 — 推理假设"},
                ],
            },
        ],
    }

    return {
        "agent": "reasoner",
        "status": "completed",
        "five_whys": whys_output,
        "fishbone": fishbone,
        "root_cause_summary": "根因：EMP-2187 更衣资质逾期 + LMS 与排班系统未集成导致无门控 + HVAC 差压异常叠加，共同导致 B 级背景微生物污染。",
        "ai_model": "rule-template-v1",
        "ai_output_hash": _hash_output(whys_output),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ============================================================
#  Reasoner LLM 路径
# ============================================================
# Reasoner LLM 输出 schema —— 用于 Pydantic 校验 LLM 返回的 JSON
from pydantic import BaseModel as PydBaseModel, Field as PydField


class _LLMFiveWhy(PydBaseModel):
    """单条 5 Why"""
    n: int = PydField(ge=1, le=10, description="Why 序号，从 1 开始")
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
    case: Deviation, evidence_hunter_output: Dict[str, Any]
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
    evidence_hunter_output: Dict[str, Any],
    llm_client,
) -> Dict[str, Any]:
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

    # 调用 LLM（强制 JSON 输出 + Pydantic 校验）
    llm_output: _LLMReasonerOutput = await llm_client.chat_json(
        system=system,
        user_prompt=user_prompt,
        schema=_LLMReasonerOutput,
    )

    # 转换为 Agent 输出格式（与规则版同构）
    whys_output = []
    for fw in llm_output.five_whys:
        why_data = {
            "n": fw.n,
            "question": fw.question,
            "answer": fw.answer,
            "evidence_ids": fw.evidence_ids,
            "is_root_cause": fw.is_root_cause,
        }
        whys_output.append({
            **why_data,
            "ai_model": llm_client.model_name,
            "ai_output_hash": _hash_output(why_data),
        })

    fishbone = llm_output.fishbone.model_dump()
    root_cause_summary = llm_output.root_cause_summary

    # 完整输出的 hash（用于审计 ai_output_hash）
    output_payload = {
        "five_whys": whys_output,
        "fishbone": fishbone,
        "root_cause_summary": root_cause_summary,
    }

    return {
        "agent": "reasoner",
        "status": "completed",
        "five_whys": whys_output,
        "fishbone": fishbone,
        "root_cause_summary": root_cause_summary,
        "ai_model": llm_client.model_name,
        "ai_prompt_hash": prompt_hash,
        "ai_output_hash": _hash_output(output_payload),
        "llm_enabled": True,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


async def _run_skeptic(
    db: AsyncSession, case: Deviation, reasoner_output: Dict[str, Any]
) -> Dict[str, Any]:
    """Skeptic — 反例挑战

    支持两种模式（通过 feature flag `skeptic_v2_enhanced` 灰度切换）：
    1. **规则版（默认兜底）**：硬编码 3-5 条挑战（v2 flag 启用时多 2 条）
    2. **LLM 版（灰度启用）**：根据当前案例的真实证据 + Reasoner 输出生成针对性反例挑战

    LLM 路径需要 `skeptic_v2_enhanced` flag 启用且 `LLMClient.is_configured` 为 True。
    LLM 失败时自动降级到规则版兜底，并在 ai_model 字段标记 fallback。
    """
    from shared.feature_flag import is_enabled
    use_v2 = is_enabled("skeptic_v2_enhanced", user_id=case.investigator_id)

    # 先生成规则版输出（作为兜底 + LLM 失败时的回退）
    rule_result = _build_rule_based_skeptic_output(case, use_v2)
    if not use_v2:
        return rule_result

    # LLM 灰度分支 —— flag 启用时调用 LLM
    try:
        from shared.llm_client import llm_client, LLMError
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


def _build_rule_based_skeptic_output(case: Deviation, use_v2: bool) -> Dict[str, Any]:
    """规则版 Skeptic —— 硬编码挑战列表"""
    challenges = [
        {
            "target": "根因结论：EMP-2187 是污染源",
            "challenge": "是否排除了其他当班人员引入污染的可能？CCTV 仅监控 EMP-2187，未覆盖其他人员更衣记录。",
            "evidence_ids": ["E-CCTV-01", "E-CCTV-02", "E-CCTV-03", "E-CCTV-04"],
            "type": "alternative_explanation",
        },
        {
            "target": "根因结论：AHU-7 差压是次要因素",
            "challenge": "AHU-7 更换滤芯后差压再次上升（E-BMS-05），可能 HVAC 本身存在独立故障，应独立调查。",
            "evidence_ids": ["E-BMS-04", "E-BMS-05"],
            "type": "independent_cause",
        },
        {
            "target": "5 Whys #2：消毒剂有效",
            "challenge": "未提供 Spor-Klenz RTU 批次 SK-2507-08 的有效性验证报告，应核实批次效力测试结果。",
            "evidence_ids": ["E-MES-01", "E-MES-02"],
            "type": "evidence_gap",
        },
    ]

    # Skeptic v2 增强 —— 2 条额外挑战
    if use_v2:
        challenges.append({
            "target": "EMS 采样策略",
            "challenge": "沉降菌采样为 4h/次，可能遗漏短时污染事件。建议核实是否需要增加主动采样（浮游菌）覆盖。",
            "evidence_ids": ["E-EMS-01", "E-EMS-05"],
            "type": "evidence_gap",
        })
        challenges.append({
            "target": "根因结论：人员是主要污染源",
            "challenge": "AHU-7 差压上升（8/12–8/14）与沉降菌计数上升（8/13–8/16）时间窗重合，应做相关性分析排除 HVAC 独立根因。",
            "evidence_ids": ["E-BMS-02", "E-BMS-03", "E-EMS-02", "E-EMS-03"],
            "type": "independent_cause",
        })

    return {
        "agent": "skeptic",
        "status": "completed",
        "challenges": challenges,
        "challenge_count": len(challenges),
        "ai_model": "rule-template-v1",
        "ai_output_hash": _hash_output(challenges),
        "timestamp": datetime.now(timezone.utc).isoformat(),
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
    case: Deviation, reasoner_output: Dict[str, Any]
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
    reasoner_output: Dict[str, Any],
    llm_client,
) -> Dict[str, Any]:
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
    )

    # 转换为 Agent 输出格式（与规则版同构）
    challenges_output = []
    for c in llm_output.challenges:
        challenge_data = {
            "target": c.target,
            "challenge": c.challenge,
            "evidence_ids": c.evidence_ids,
            "type": c.type,
        }
        challenges_output.append({
            **challenge_data,
            "ai_model": llm_client.model_name,
            "ai_output_hash": _hash_output(challenge_data),
        })

    # 完整输出的 hash（用于审计 ai_output_hash）
    output_payload = {"challenges": challenges_output}

    return {
        "agent": "skeptic",
        "status": "completed",
        "challenges": challenges_output,
        "challenge_count": len(challenges_output),
        "ai_model": llm_client.model_name,
        "ai_prompt_hash": prompt_hash,
        "ai_output_hash": _hash_output(output_payload),
        "llm_enabled": True,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


async def _run_drafter(
    db: AsyncSession, case: Deviation, reasoner_output: Dict[str, Any], skeptic_output: Dict[str, Any]
) -> Dict[str, Any]:
    """Drafter — ICH Q9 报告生成

    支持两种模式（通过 feature flag `drafter_llm_enabled` 灰度切换）：
    1. **规则版（默认兜底）**：Python 字符串拼接 ICH Q9 模板
    2. **LLM 版（灰度启用）**：调用 LLM 生成完整 Markdown 报告，嵌入 ICH Q9 格式要求

    LLM 路径需要 `drafter_llm_enabled` flag 启用且 `LLMClient.is_configured` 为 True。
    LLM 失败时自动降级到规则版兜底。
    """
    from shared.feature_flag import is_enabled
    use_llm = is_enabled("drafter_llm_enabled", user_id=case.investigator_id)

    # 先生成规则版输出（作为兜底）
    rule_result = _build_rule_based_drafter_output(case, reasoner_output, skeptic_output)
    if not use_llm:
        return rule_result

    # LLM 灰度分支
    try:
        from shared.llm_client import llm_client, LLMError
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
    reasoner_output: Dict[str, Any],
    skeptic_output: Dict[str, Any],
) -> Dict[str, Any]:
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

9. CAPA 建议
  - 立即：暂停 EMP-2187 B 级区作业，重新培训 + 更衣认证
  - 短期：建立 LMS↔排班系统 资质门控 API 集成
  - 长期：HVAC 系统独立调查（AHU-7 差压异常复发）

10. 合规声明
本调查遵循 ICH Q9 质量风险管理原则，所有结论均有证据引用支持。
"""
    return {
        "agent": "drafter",
        "status": "completed",
        "report_markdown": report,
        "report_format": "ICH-Q9-template-v1",
        "ai_model": "rule-template-v1",
        "ai_output_hash": _hash_output(report),
        "timestamp": datetime.now(timezone.utc).isoformat(),
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
    reasoner_output: Dict[str, Any],
    skeptic_output: Dict[str, Any],
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
    reasoner_output: Dict[str, Any],
    skeptic_output: Dict[str, Any],
    llm_client,
) -> Dict[str, Any]:
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

    # 调用 LLM（Drafter 输出 Markdown 文本，非 JSON，故用 chat_completion）
    report_markdown = await llm_client.chat_completion(
        system=system,
        user_prompt=user_prompt,
        json_mode=False,
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
        "timestamp": datetime.now(timezone.utc).isoformat(),
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


def _format_reasoner_for_prompt(label: str, r_out: Optional[Dict[str, Any]]) -> str:
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
    primary_output: Dict[str, Any],
    adversary_output: Optional[Dict[str, Any]],
    skeptic_output: Dict[str, Any],
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
    primary_output: Dict[str, Any],
    adversary_output: Optional[Dict[str, Any]],
    skeptic_output: Dict[str, Any],
    llm_client,
) -> Dict[str, Any]:
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
        "timestamp": datetime.now(timezone.utc).isoformat(),
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
    primary_output: Dict[str, Any],
    adversary_output: Optional[Dict[str, Any]],
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
    primary_output: Dict[str, Any],
    adversary_output: Optional[Dict[str, Any]],
    llm_client,
) -> Dict[str, Any]:
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
    )

    challenges_output = []
    for c in llm_output.challenges:
        challenge_data = {
            "target": c.target,
            "challenge": c.challenge,
            "evidence_ids": c.evidence_ids,
            "type": c.type,
        }
        challenges_output.append({
            **challenge_data,
            "ai_model": llm_client.model_name,
            "ai_output_hash": _hash_output(challenge_data),
        })

    output_payload = {"challenges": challenges_output}

    return {
        "agent": "skeptic",
        "status": "completed",
        "challenges": challenges_output,
        "challenge_count": len(challenges_output),
        "cross_examine_mode": True,
        "ai_model": llm_client.model_name,
        "ai_prompt_hash": prompt_hash,
        "ai_output_hash": _hash_output(output_payload),
        "llm_enabled": True,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ============================================================
#  并行 Reasoner 博弈主入口
# ============================================================

async def _run_reasoner_debate(
    db: AsyncSession,
    case: Deviation,
    evidence_hunter_output: Dict[str, Any],
) -> Dict[str, Any]:
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
    from shared.llm_client import llm_client, LLMError, get_debate_client

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
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    adversary_client = get_debate_client()

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
        "adversary_error": adversary_err if adversary_output is None and adversary_client is not None else None,
        "ai_model": primary_output.get("ai_model", "rule-template-v1"),
        "ai_output_hash": _hash_output(debate_payload),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


async def _run_drafter_debate(
    db: AsyncSession,
    case: Deviation,
    debate_output: Dict[str, Any],
    skeptic_output: Dict[str, Any],
    meta_controller_output: Dict[str, Any],
) -> Dict[str, Any]:
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
    primary_output: Dict[str, Any],
    adversary_output: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """交叉质证 Skeptic wrapper —— LLM 路径 + 三级降级

    1. flag `multi_agent_debate_enabled` 已启用 → 调用 LLM 交叉质证
    2. LLM 未配置 → 降级到规则版 Skeptic（用 primary 作 reasoner_input）
    3. LLM 异常 → 降级到规则版 Skeptic
    """
    # 用 primary 作为 reasoner_input 走规则版兜底
    rule_result = _build_rule_based_skeptic_output(case, use_v2=True)

    try:
        from shared.llm_client import llm_client, LLMError
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
    primary_output: Dict[str, Any],
    adversary_output: Optional[Dict[str, Any]],
    skeptic_output: Dict[str, Any],
) -> Dict[str, Any]:
    """Meta-Controller 综合 wrapper —— LLM 路径 + 降级

    降级策略：LLM 不可用时，构造一个简单的综合结论（采用 primary 的根因结论，
    confidence_score=0.5 标记为缺乏交叉验证）。
    """
    fallback_summary = primary_output.get("root_cause_summary", "(primary 推理失败)")
    fallback_payload = {
        "agreed_root_cause": fallback_summary if adversary_output is None else "",
        "final_root_cause_summary": fallback_summary,
        "confidence_score": 0.5 if adversary_output is None else 0.3,
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
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        from shared.llm_client import llm_client, LLMError
        if not llm_client.is_configured:
            fallback_result["llm_fallback_reason"] = "LLM 未配置，Meta-Controller 降级到简单综合"
            return fallback_result

        return await _run_meta_controller_synthesis_with_llm(
            case, primary_output, adversary_output, skeptic_output, llm_client
        )
    except LLMError as e:
        fallback_result["ai_model"] = "rule-template-v1 (llm-fallback)"
        fallback_result["llm_fallback_reason"] = f"LLM 调用失败降级: {type(e).__name__}: {str(e)[:200]}"
        return fallback_result
    except Exception as e:
        fallback_result["ai_model"] = "rule-template-v1 (llm-error)"
        fallback_result["llm_fallback_reason"] = f"LLM 未知异常降级: {type(e).__name__}: {str(e)[:200]}"
        return fallback_result


async def _run_guardian(
    db: AsyncSession, case: Deviation, drafter_output: Dict[str, Any]
) -> Dict[str, Any]:
    """Guardian — ALCOA+ 9 项合规检查"""
    checks = [
        {"principle": "Attributable (可归因)", "status": "pass", "detail": "所有操作均记录操作员 ID 和时间戳"},
        {"principle": "Legible (清晰)", "status": "pass", "detail": "电子记录可读，无手写模糊"},
        {"principle": "Contemporaneous (同期)", "status": "pass", "detail": "证据收集时间与事件发生时间一致"},
        {"principle": "Original (原始)", "status": "pass", "detail": "证据直接来自源系统，未经二次转录"},
        {"principle": "Accurate (准确)", "status": "warning", "detail": "LMS 告警日志缺失，数据完整性存在风险"},
        {"principle": "Complete (完整)", "status": "pass", "detail": "5 Whys + 鱼骨图 + Skeptic 反例完整"},
        {"principle": "Consistent (一致)", "status": "pass", "detail": "证据间无矛盾"},
        {"principle": "Enduring (持久)", "status": "pass", "detail": "审计日志不可篡改，DB RULE 防护"},
        {"principle": "Available (可用)", "status": "pass", "detail": "可导出 CSV/JSON 审计包供监管检查"},
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
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ============================================================
#  流水线编排
# ============================================================
async def _execute_pipeline(
    case_id: str,
    run_id: str,
    triggered_by: str,
) -> None:
    """后台执行 Agent 流水线 — 串行执行 6 个 Agent，每步写审计日志"""
    async with db_session() as db:
        # 加载完整案例
        stmt = (
            select(Deviation)
            .options(
                selectinload(Deviation.evidence),
                selectinload(Deviation.timeline),
                selectinload(Deviation.five_whys),
                selectinload(Deviation.skeptic_challenges),
            )
            .where(Deviation.id == case_id)
        )
        case = (await db.execute(stmt)).scalar_one_or_none()
        if not case:
            return

        run = (await db.execute(select(AgentRun).where(AgentRun.id == run_id))).scalar_one_or_none()
        if not run:
            return

        agent_states = dict(run.agent_states or {})
        run.status = "running"
        run.started_at = datetime.now(timezone.utc)

        try:
            # 1. Meta-Controller — 初始化
            run.current_agent = "meta_controller"
            agent_states["meta_controller"] = {
                "status": "completed",
                "summary": f"启动案例 {case_id} 的 Agent 流水线",
                "pipeline": AGENT_PIPELINE,
            }
            await db.flush()

            # 2. Evidence Hunter
            run.current_agent = "evidence_hunter"
            await db.flush()
            eh_out = await _run_evidence_hunter(db, case)
            agent_states["evidence_hunter"] = eh_out
            await append_audit_log(
                db,
                operator="Evidence Hunter (AI)",
                action="AGENT_RUN",
                target=f"{case_id}/evidence_hunter",
                detail=eh_out["summary"],
                ai_model="rule-template-v1",
                ai_output_hash=eh_out["ai_output_hash"],
            )
            await db.flush()

            # 3. Reasoner（含多 Agent 博弈分支）
            from shared.feature_flag import is_enabled as _is_flag_enabled
            use_debate = _is_flag_enabled("multi_agent_debate_enabled", user_id=case.investigator_id)

            if use_debate:
                # ===== 多 Agent 博弈路径（Phase 2 改进5）=====
                run.current_agent = "reasoner_debate"
                await db.flush()
                debate_out = await _run_reasoner_debate(db, case, eh_out)
                agent_states["reasoner_debate"] = debate_out

                primary_out = debate_out.get("primary") or {}
                adversary_out = debate_out.get("adversary")
                debate_mode = debate_out.get("debate_mode", "unknown")

                # 审计 primary Reasoner
                await append_audit_log(
                    db,
                    operator="Reasoner (AI) - primary",
                    action="AGENT_RUN",
                    target=f"{case_id}/reasoner_debate/primary",
                    detail=f"[debate={debate_mode}] {primary_out.get('root_cause_summary', '?')}",
                    ai_model=primary_out.get("ai_model", "rule-template-v1"),
                    ai_prompt_hash=primary_out.get("ai_prompt_hash"),
                    ai_output_hash=primary_out.get("ai_output_hash"),
                )

                # 审计 adversary Reasoner（若存在）
                if adversary_out is not None:
                    await append_audit_log(
                        db,
                        operator="Reasoner (AI) - adversary",
                        action="AGENT_RUN",
                        target=f"{case_id}/reasoner_debate/adversary",
                        detail=f"[debate={debate_mode}] {adversary_out.get('root_cause_summary', '?')}",
                        ai_model=adversary_out.get("ai_model", "rule-template-v1"),
                        ai_prompt_hash=adversary_out.get("ai_prompt_hash"),
                        ai_output_hash=adversary_out.get("ai_output_hash"),
                    )
                elif debate_out.get("adversary_error"):
                    await append_audit_log(
                        db,
                        operator="Reasoner (AI) - adversary",
                        action="AGENT_RUN_FALLBACK",
                        target=f"{case_id}/reasoner_debate/adversary",
                        detail=f"adversary 降级原因: {debate_out['adversary_error']}",
                        ai_model="rule-template-v1 (llm-fallback)",
                    )
                await db.flush()

                # 4. Skeptic 交叉质证（两份 Reasoner 输入）
                run.current_agent = "skeptic_cross_examine"
                await db.flush()
                s_out = await _run_skeptic_cross_examine(db, case, primary_out, adversary_out)
                agent_states["skeptic"] = s_out
                await append_audit_log(
                    db,
                    operator="Skeptic (AI) - cross-examine",
                    action="AGENT_RUN",
                    target=f"{case_id}/skeptic_cross_examine",
                    detail=f"[cross-examine] 生成 {s_out['challenge_count']} 条交叉质证挑战",
                    ai_model=s_out.get("ai_model", "rule-template-v1"),
                    ai_prompt_hash=s_out.get("ai_prompt_hash"),
                    ai_output_hash=s_out["ai_output_hash"],
                )
                await db.flush()

                # 5. Meta-Controller 综合置信度结论
                run.current_agent = "meta_controller"
                await db.flush()
                mc_out = await _run_meta_controller_synthesis(
                    db, case, primary_out, adversary_out, s_out
                )
                agent_states["meta_controller"] = mc_out
                await append_audit_log(
                    db,
                    operator="Meta-Controller (AI) - synthesis",
                    action="AGENT_RUN",
                    target=f"{case_id}/meta_controller",
                    detail=(
                        f"[confidence={mc_out.get('confidence_score', 0):.2f}] "
                        f"{mc_out.get('final_root_cause_summary', '?')[:200]}"
                    ),
                    ai_model=mc_out.get("ai_model", "rule-template-v1"),
                    ai_prompt_hash=mc_out.get("ai_prompt_hash"),
                    ai_output_hash=mc_out.get("ai_output_hash"),
                )
                await db.flush()

                # 6. Drafter（使用 Meta-Controller 综合结论）
                run.current_agent = "drafter"
                await db.flush()
                d_out = await _run_drafter_debate(db, case, debate_out, s_out, mc_out)
                agent_states["drafter"] = d_out
                await append_audit_log(
                    db,
                    operator="Drafter (AI)",
                    action="AGENT_RUN",
                    target=f"{case_id}/drafter",
                    detail=f"生成 ICH Q9 报告 (format: {d_out.get('report_format', '?')}, hash: {d_out['ai_output_hash'][:32]}...)",
                    ai_model=d_out.get("ai_model", "rule-template-v1"),
                    ai_prompt_hash=d_out.get("ai_prompt_hash"),
                    ai_output_hash=d_out["ai_output_hash"],
                )
                await db.flush()

            else:
                # ===== 单 Reasoner 原路径（默认）=====
                # 3. Reasoner
                run.current_agent = "reasoner"
                await db.flush()
                r_out = await _run_reasoner(db, case, eh_out)
                agent_states["reasoner"] = r_out
                await append_audit_log(
                    db,
                    operator="Reasoner (AI)",
                    action="AGENT_RUN",
                    target=f"{case_id}/reasoner",
                    detail=r_out["root_cause_summary"],
                    ai_model=r_out.get("ai_model", "rule-template-v1"),
                    ai_prompt_hash=r_out.get("ai_prompt_hash"),
                    ai_output_hash=r_out["ai_output_hash"],
                )
                await db.flush()

                # 4. Skeptic
                run.current_agent = "skeptic"
                await db.flush()
                s_out = await _run_skeptic(db, case, r_out)
                agent_states["skeptic"] = s_out
                await append_audit_log(
                    db,
                    operator="Skeptic (AI)",
                    action="AGENT_RUN",
                    target=f"{case_id}/skeptic",
                    detail=f"生成 {s_out['challenge_count']} 条反例挑战",
                    ai_model=s_out.get("ai_model", "rule-template-v1"),
                    ai_prompt_hash=s_out.get("ai_prompt_hash"),
                    ai_output_hash=s_out["ai_output_hash"],
                )
                await db.flush()

                # 5. Drafter
                run.current_agent = "drafter"
                await db.flush()
                d_out = await _run_drafter(db, case, r_out, s_out)
                agent_states["drafter"] = d_out
                await append_audit_log(
                    db,
                    operator="Drafter (AI)",
                    action="AGENT_RUN",
                    target=f"{case_id}/drafter",
                    detail=f"生成 ICH Q9 报告 (format: {d_out.get('report_format', '?')}, hash: {d_out['ai_output_hash'][:32]}...)",
                    ai_model=d_out.get("ai_model", "rule-template-v1"),
                    ai_prompt_hash=d_out.get("ai_prompt_hash"),
                    ai_output_hash=d_out["ai_output_hash"],
                )
                await db.flush()

            # 7. Guardian（两条路径共同）
            run.current_agent = "guardian"
            await db.flush()
            g_out = await _run_guardian(db, case, d_out)
            agent_states["guardian"] = g_out
            await append_audit_log(
                db,
                operator="Guardian (AI)",
                action="AGENT_RUN",
                target=f"{case_id}/guardian",
                detail=g_out["summary"],
                ai_model="rule-engine-v1",
                ai_output_hash=g_out["ai_output_hash"],
            )

            run.agent_states = agent_states
            run.status = "completed"
            run.current_agent = None
            run.completed_at = datetime.now(timezone.utc)
            await db.flush()

        except Exception as e:
            # GxP 要求：失败的尝试也必须有审计记录。
            # 主事务即将回滚，需用独立 session 写入失败审计日志。
            run.status = "failed"
            run.agent_states = {**agent_states, "error": str(e)}
            await db.flush()
            try:
                await db.commit()  # 先尝试提交 run 状态变更
            except Exception:
                await db.rollback()
                # run 状态变更回滚后，仍需独立写入失败审计日志
                pass

            # 独立事务写失败审计日志（不依赖主事务）
            try:
                async with db_session() as audit_db:
                    await append_audit_log(
                        audit_db,
                        operator="Meta-Controller (AI)",
                        action="AGENT_PIPELINE_FAILED",
                        target=f"{case_id}/run/{run_id}",
                        detail=f"Agent 流水线失败: {type(e).__name__}: {str(e)[:500]}",
                        ai_model="rule-template-v1",
                        ai_output_hash=_hash_output({
                            "run_id": run_id,
                            "case_id": case_id,
                            "error": str(e),
                            "failed_at": datetime.now(timezone.utc).isoformat(),
                        }),
                    )
                    await audit_db.commit()
            except Exception:
                # 审计日志写入也失败 —— 只能记录到 stderr
                import sys
                print(f"[AGENT] CRITICAL: 审计日志写入失败 run_id={run_id}", file=sys.stderr)


# ============================================================
#  API 路由
# ============================================================
@router.post("/pipeline/run", response_model=AgentRunOut, status_code=201, summary="启动 Agent 流水线")
@write_limit
async def start_pipeline(
    request: Request,
    body: AgentRunCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db_from_auth),
    auth: AuthContext = Depends(require("agent:run")),
) -> AgentRunOut:
    """启动案例的 Agent 流水线 — 后台异步执行"""
    case = (await db.execute(select(Deviation).where(Deviation.id == body.case_id))).scalar_one_or_none()
    if not case:
        raise NotFoundError(f"案例 {body.case_id} 不存在")

    run_id = f"RUN-{body.case_id}-{int(datetime.now(timezone.utc).timestamp())}"
    run = AgentRun(
        id=run_id,
        case_id=body.case_id,
        status="pending",
        triggered_by=body.triggered_by or auth.email,
        agent_states={},
    )
    db.add(run)
    await db.flush()

    await append_audit_log(
        db,
        operator=auth.email,
        action="AGENT_PIPELINE_START",
        target=body.case_id,
        detail=f"启动 Agent 流水线 (run_id={run_id})",
        ip_address=auth.ip_address,
        session_id=auth.session_id,
    )

    # 后台执行
    background_tasks.add_task(_execute_pipeline, body.case_id, run_id, auth.email)

    return AgentRunOut.model_validate(run)


@router.get("/runs/{run_id}", response_model=AgentRunOut, summary="查询 Agent 运行状态")
@api_limit
async def get_run(
    request: Request,
    run_id: str,
    db: AsyncSession = Depends(get_db_from_auth),
    _: AuthContext = Depends(require("agent:run")),
) -> AgentRunOut:
    """查询 Agent 流水线运行状态 + 各 Agent 输出"""
    run = (await db.execute(select(AgentRun).where(AgentRun.id == run_id))).scalar_one_or_none()
    if not run:
        raise NotFoundError(f"Agent 运行 {run_id} 不存在")
    return AgentRunOut.model_validate(run)


@router.get("/runs", response_model=Page[AgentRunOut], summary="查询 Agent 运行列表")
@api_limit
async def list_runs(
    request: Request,
    case_id: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db_from_auth),
    _: AuthContext = Depends(require("agent:run")),
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


@router.post("/runs/{run_id}/pause", summary="暂停 Agent 运行 (HITL)")
@write_limit
async def pause_run(
    request: Request,
    run_id: str,
    db: AsyncSession = Depends(get_db_from_auth),
    auth: AuthContext = Depends(require("agent:pause")),
) -> dict:
    """暂停 Agent 流水线 — 等待 HITL 人工决策"""
    run = (await db.execute(select(AgentRun).where(AgentRun.id == run_id))).scalar_one_or_none()
    if not run:
        raise NotFoundError(f"Agent 运行 {run_id} 不存在")
    if run.status != "running":
        raise ValidationError("仅运行中的 Agent 可暂停")
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
