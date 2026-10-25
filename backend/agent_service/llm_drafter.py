"""Drafter：ICH Q9 调查报告起草（规则版 / LLM 版）

P3-① 自 agent_service/routes.py 拆出（机械搬运，正文与原文件逐字一致）。
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import settings
from shared.database import release_db_connection
from shared.models import Deviation

from .agents import _hash_output
from .prompts import _DRAFTER_SYSTEM_PROMPT, PROMPT_VERSIONS, _build_drafter_user_prompt


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
    from shared.feature_flag import is_enabled  # 调用时绑定
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

        # C3：整篇报告生成耗时最长，务必先归还 DB 连接
        await release_db_connection(db)
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
        "prompt_version": PROMPT_VERSIONS["drafter"],
        "llm_enabled": True,
        "timestamp": datetime.now(UTC).isoformat(),
    }


