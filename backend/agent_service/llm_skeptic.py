"""Skeptic：对抗性质询 + 交叉质证（规则版 / LLM 版）

P3-① 自 agent_service/routes.py 拆出（机械搬运，正文与原文件逐字一致）。
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel as PydBaseModel
from pydantic import Field as PydField
from sqlalchemy.ext.asyncio import AsyncSession

from shared.database import release_db_connection
from shared.models import Deviation

from .agents import _case_evidence_ids, _filter_hallucinated_evidence, _hash_output
from .prompts import (
    _CROSS_EXAMINE_SYSTEM_PROMPT,
    _SKEPTIC_SYSTEM_PROMPT,
    PROMPT_VERSIONS,
    _build_cross_examine_user_prompt,
    _build_skeptic_user_prompt,
)


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
    from shared.feature_flag import is_enabled  # 调用时绑定
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

        # C3：LLM 调用前归还 DB 连接（prompt 数据已全部读入内存）
        await release_db_connection(db)
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
        "prompt_version": PROMPT_VERSIONS["skeptic"],
        "llm_enabled": True,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    if hallucinated:
        result["hallucination_filtered"] = sorted(set(hallucinated))
    return result



# ============================================================
#  交叉质证 System Prompt（Skeptic 扩展，支持双 Reasoner 输入）
# ============================================================

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
        "prompt_version": PROMPT_VERSIONS["cross_examine"],
        "llm_enabled": True,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    if hallucinated:
        result["hallucination_filtered"] = sorted(set(hallucinated))
    return result



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

    # C3：交叉质证 LLM 调用前归还 DB 连接（输入全部来自内存数据）
    await release_db_connection(db)

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


