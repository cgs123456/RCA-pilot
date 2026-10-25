"""Multi-Agent 博弈：双 Reasoner 辩论 + Meta-Controller 置信度综合

P3-① 自 agent_service/routes.py 拆出（机械搬运，正文与原文件逐字一致）。
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel as PydBaseModel
from pydantic import Field as PydField
from sqlalchemy.ext.asyncio import AsyncSession

from shared.database import release_db_connection
from shared.models import Deviation

from .agents import _hash_output
from .llm_drafter import _run_drafter
from .llm_reasoner import _build_rule_based_reasoner_output, _run_reasoner_with_llm
from .prompts import (
    _META_CONTROLLER_SYSTEM_PROMPT,
    PROMPT_VERSIONS,
    _build_meta_controller_user_prompt,
)

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
        "prompt_version": PROMPT_VERSIONS["meta_controller"],
        "llm_enabled": True,
        "timestamp": datetime.now(UTC).isoformat(),
    }



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

    from shared.llm_client import LLMError, get_debate_client, llm_client  # 调用时绑定

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
    # C3：博弈是最长的 LLM 段（两条并发调用），先把 db 连接归还池
    # （case/evidence 已全部读入内存，db 在本函数内不再有读库操作）
    await release_db_connection(db)
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

        # C3：LLM 综合前归还 DB 连接
        await release_db_connection(db)
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


