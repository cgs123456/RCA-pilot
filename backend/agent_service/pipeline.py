"""流水线编排：阶段执行、审计留痕、指标、僵尸恢复、DB 轮询 worker 取件

P3-① 自 agent_service/routes.py 拆出（机械搬运，正文与原文件逐字一致）。
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from shared.audit import append_audit_log
from shared.database import db_session
from shared.metrics import AGENT_RUNS_TOTAL, AGENT_STAGE_DURATION
from shared.models import AgentRun, Deviation
from shared.notify import create_notification

from .agents import _hash_output, _run_evidence_hunter, _run_guardian
from .llm_debate import _run_drafter_debate, _run_meta_controller_synthesis, _run_reasoner_debate
from .llm_drafter import _run_drafter
from .llm_reasoner import _run_reasoner
from .llm_skeptic import _run_skeptic, _run_skeptic_cross_examine

logger = logging.getLogger("agent-service")

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
    """启动时恢复 —— 把中断遗留的 running run 重新排队为 pending（C7）

    GxP 要求中断尝试也必须留痕：进程重启后 BackgroundTasks/worker 里的流水线已丢失，
    重启恢复把 stale running 重新排队（worker 会自动续跑，语义与 resume 一致：各阶段
    重算并覆盖输出），并写入 REQUEUED 审计。pending run 不在恢复范围 —— 那是 worker
    的正常队列。
    返回重新排队的 run 数量。
    """

    threshold = datetime.now(UTC) - timedelta(minutes=ZOMBIE_RUN_THRESHOLD_MINUTES)
    requeued = 0
    async with db_session() as db:
        runs = (await db.execute(
            select(AgentRun).where(AgentRun.status == "running")
        )).scalars().all()
        for run in runs:
            ref = _aware(run.started_at) or _aware(run.created_at)
            # 刚启动不久的 run 保守跳过（多实例部署时其他实例可能正在执行）
            if ref and ref > threshold:
                continue
            run.status = "pending"
            run.started_at = None
            run.current_agent = None
            await append_audit_log(
                db,
                operator="system:startup-recovery",
                action="AGENT_PIPELINE_REQUEUED",
                target=f"{run.case_id}/run/{run.id}",
                detail="服务重启导致流水线中断，已重新排队等待执行（C7: 流水线持久化）",
                ai_model="rule-engine-v1",
            )
            requeued += 1
    if requeued:
        logger.info("startup recovery: %d interrupted run(s) requeued", requeued)
    return requeued


async def _execute_pipeline(
    case_id: str, run_id: str, triggered_by: str, *, claimed: bool = False
) -> None:
    """后台执行 Agent 流水线 — 串行执行 6 个 Agent，每阶段独立事务提交

    claimed=False（BackgroundTasks 快路径）时先原子认领：run 已被 worker 或其他
    执行方认领（status!=pending）则直接退出，保证双路径幂等。
    """
    import time

    if not claimed:
        from agent_service.worker import claim_pending_run

        if not await claim_pending_run(run_id):
            return
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
        assert eh_out is not None
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
            assert debate_out is not None
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
            assert s_out is not None
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
            assert mc_out is not None
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
            assert d_out is not None
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
            assert r_out is not None
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
            assert s_out is not None
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
            assert d_out is not None
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
        assert g_out is not None
        _observe_stage("guardian", g_out or {}, time.perf_counter() - t0)
        await _write_stage_result(
            run_id, agent="guardian", states_update={"guardian": g_out},
            audits=[_stage_audit("Guardian (AI)", f"{case_id}/guardian", g_out["summary"], g_out)],
            completed=True,
        )
        AGENT_RUNS_TOTAL.labels(status="completed").inc()

        # P2-⑧ 流水线完成 → 通知调查员与 QA 审核人进入 HITL 审批
        try:
            async with db_session() as ndb:
                row = (await ndb.execute(
                    select(Deviation.investigator_id, Deviation.qa_reviewer_id, Deviation.title)
                    .where(Deviation.id == case_id)
                )).first()
                if row:
                    for recipient in {row.investigator_id, row.qa_reviewer_id} - {None}:
                        await create_notification(
                            ndb,
                            recipient,
                            "GATE_READY",
                            case_id=case_id,
                            payload={"title": row.title, "run_id": run_id},
                        )
                    await ndb.commit()
        except Exception:
            logger.warning("GATE_READY 通知写入失败（不影响流水线结果）", exc_info=True)

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


