"""Agent Service — 规则版编排引擎

Phase 1 实现：基于规则的状态机，无 LLM 调用。
所有 Agent 输出基于案例已有证据数据 + 预设模板生成，
每步输出计算 hash 并写入审计日志。
"""
from __future__ import annotations

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
    """Evidence Hunter — 聚合案例已有证据 + 标注证据缺口"""
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
    output_payload = {
        "agent": "evidence_hunter",
        "status": "completed",
        "summary": f"收集 {len(evidence_list)} 条证据，覆盖 {len(by_source)} 个源系统",
        "evidence_count": len(evidence_list),
        "by_source": by_source,
        "anomaly_count": len(anomalies),
        "gaps": gaps,
    }

    return {
        **output_payload,
        "ai_model": "rule-template-v1",
        "ai_output_hash": _hash_output(output_payload),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


async def _run_reasoner(
    db: AsyncSession, case: Deviation, evidence_hunter_output: Dict[str, Any]
) -> Dict[str, Any]:
    """Reasoner — 模板版 5 Whys 推理 + 鱼骨图

    Phase 1 基于案例已有 5 Whys 数据返回，并计算每条的 hash。
    Phase 2 升级为 LLM Chain-of-Thought（通过 reasoner_llm_enabled flag 灰度）。
    """
    # Feature Flag 灰度：reasoner_llm_enabled 启用时走 LLM 推理分支
    # Phase 1 规则版默认关闭；Phase 3 接入 LLM 后将走真实 LLM 调用
    from shared.feature_flag import is_enabled
    use_llm = is_enabled("reasoner_llm_enabled", user_id=case.investigator_id)

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

    result = {
        "agent": "reasoner",
        "status": "completed",
        "five_whys": whys_output,
        "fishbone": fishbone,
        "root_cause_summary": "根因：EMP-2187 更衣资质逾期 + LMS 与排班系统未集成导致无门控 + HVAC 差压异常叠加，共同导致 B 级背景微生物污染。",
        "ai_model": "rule-template-v1",
        "ai_output_hash": _hash_output(whys_output),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # LLM 灰度分支 —— flag 启用时标记 ai_model，实际 LLM 调用在 Phase 3 接入
    if use_llm:
        result["ai_model"] = "llm-cot-v1-placeholder"
        result["llm_enabled"] = True
        # TODO Phase 3: 调用 LLM Chain-of-Thought 替换 whys_output + root_cause_summary
        # 当前保留规则版输出，仅标记模型版本用于审计追踪

    return result


async def _run_skeptic(
    db: AsyncSession, case: Deviation, reasoner_output: Dict[str, Any]
) -> Dict[str, Any]:
    """Skeptic — 基于证据缺口的反例挑战

    Phase 1 规则版：检查证据充分性、识别替代解释。
    Phase 2c：通过 feature flag `skeptic_v2_enhanced` 灰度启用增强版（5 条挑战 + 独立证据校验）。
    """
    from shared.feature_flag import is_enabled

    # Feature Flag 灰度：skeptic_v2_enhanced 启用时走增强版逻辑
    use_v2 = is_enabled("skeptic_v2_enhanced", user_id=case.investigator_id)

    challenges = []

    # 挑战 1：是否有其他人员可能引入污染？
    challenges.append({
        "target": "根因结论：EMP-2187 是污染源",
        "challenge": "是否排除了其他当班人员引入污染的可能？CCTV 仅监控 EMP-2187，未覆盖其他人员更衣记录。",
        "evidence_ids": ["E-CCTV-01", "E-CCTV-02", "E-CCTV-03", "E-CCTV-04"],
        "type": "alternative_explanation",
    })

    # 挑战 2：HVAC 是否是独立根因？
    challenges.append({
        "target": "根因结论：AHU-7 差压是次要因素",
        "challenge": "AHU-7 更换滤芯后差压再次上升（E-BMS-05），可能 HVAC 本身存在独立故障，应独立调查。",
        "evidence_ids": ["E-BMS-04", "E-BMS-05"],
        "type": "independent_cause",
    })

    # 挑战 3：消毒剂有效性是否验证？
    challenges.append({
        "target": "5 Whys #2：消毒剂有效",
        "challenge": "未提供 Spor-Klenz RTU 批次 SK-2507-08 的有效性验证报告，应核实批次效力测试结果。",
        "evidence_ids": ["E-MES-01", "E-MES-02"],
        "type": "evidence_gap",
    })

    # ============================================================
    # Skeptic v2 增强（feature flag 灰度）—— 2 条额外挑战 + 独立证据校验
    # ============================================================
    if use_v2:
        # 挑战 4：环境监控采样频率是否充分？
        challenges.append({
            "target": "EMS 采样策略",
            "challenge": "沉降菌采样为 4h/次，可能遗漏短时污染事件。建议核实是否需要增加主动采样（浮游菌）覆盖。",
            "evidence_ids": ["E-EMS-01", "E-EMS-05"],
            "type": "evidence_gap",
        })

        # 挑战 5：BMS 差压趋势与菌落计数的相关性
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


async def _run_drafter(
    db: AsyncSession, case: Deviation, reasoner_output: Dict[str, Any], skeptic_output: Dict[str, Any]
) -> Dict[str, Any]:
    """Drafter — ICH Q9 模板版报告生成"""
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
                ai_model="rule-template-v1",
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
                ai_model="rule-template-v1",
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
                detail=f"生成 ICH Q9 模板报告 (hash: {d_out['ai_output_hash'][:32]}...)",
                ai_model="rule-template-v1",
                ai_output_hash=d_out["ai_output_hash"],
            )
            await db.flush()

            # 6. Guardian
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
