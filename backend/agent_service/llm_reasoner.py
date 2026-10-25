"""Reasoner：5 Whys + 6M 鱼骨图（规则版 / LLM 版，feature flag 灰度）

P3-① 自 agent_service/routes.py 拆出（机械搬运，正文与原文件逐字一致）。
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel as PydBaseModel
from pydantic import Field as PydField
from sqlalchemy.ext.asyncio import AsyncSession

from shared.database import release_db_connection
from shared.models import Deviation

from .agents import _case_evidence_ids, _filter_hallucinated_evidence, _hash_output
from .prompts import _REASONER_SYSTEM_PROMPT, PROMPT_VERSIONS, _build_reasoner_user_prompt


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
    from shared.feature_flag import is_enabled  # 调用时绑定
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

        # C3：LLM 调用可能耗时数十秒，先归还 DB 连接（prompt 数据已全部读入内存）
        await release_db_connection(db)
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
        "prompt_version": PROMPT_VERSIONS["reasoner"],
        "llm_enabled": True,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    if hallucinated:
        # 幻觉证据 ID 被剔除 —— 记录到输出供审计追溯（AgentHallucinationError 不阻塞流水线）
        result["hallucination_filtered"] = sorted(set(hallucinated))
    return result


