"""Agent Service — 6 Agent 流水线编排引擎

运行时可靠性（GxP 关键）：
- 每个阶段使用独立短事务落库（进度对外可见，pause/cancel 生效）
- 流水线持久化在 DB（status=pending），BackgroundTasks 快路径 + DB 轮询 worker
  （agent_service.worker）双路径幂等认领，进程死亡后 run 不再丢失
- 进程重启后启动恢复把中断的 running 重新排队 pending（main.py lifespan）
- 异常路径用独立会话标记 failed + 补审计，run 不会永久卡在 pending
- run_id 使用 uuid 后缀，同案例并发启动有 running 守卫 + 部分唯一索引兜底

注意：本文件不使用 `from __future__ import annotations`（PEP 563）——
slowapi 包装端点的字符串注解解析会落到 slowapi 模块命名空间导致 NameError。
"""
import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Query, Request
from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.audit import append_audit_log
from shared.auth_middleware import AuthContext, get_auth_context, get_db_from_auth, require
from shared.config import settings
from shared.database import get_db
from shared.exceptions import ConflictError, NotFoundError, ValidationError
from shared.metrics import AGENT_RUNS_TOTAL
from shared.models import (
    AgentRun,
    Deviation,
)
from shared.rate_limit import api_limit, write_limit
from shared.schemas import AgentRunCreate, AgentRunOut, Page
from shared.security import safe_compare

# P3-① 拆分：Agent 实现移至同包各模块；此处保留 HTTP 端点、请求/响应模型，
# 并向后兼容地再导出旧符号（tests 与外部调用方沿用 agent_service.routes.X 引用）。
from .agents import (  # noqa: F401
    _case_evidence_ids,
    _filter_hallucinated_evidence,
    _hash_output,
    _run_evidence_hunter,
    _run_guardian,
)
from .llm_debate import (  # noqa: F401
    _build_meta_controller_user_prompt,
    _LLMMetaControllerOutput,
    _run_drafter_debate,
    _run_meta_controller_synthesis,
    _run_reasoner_debate,
)
from .llm_drafter import _run_drafter  # noqa: F401
from .llm_reasoner import (  # noqa: F401
    _build_rule_based_reasoner_output,
    _LLMFishbone,
    _LLMFishboneCategory,
    _LLMFiveWhy,
    _LLMReasonerOutput,
    _run_reasoner,
    _run_reasoner_with_llm,
)
from .llm_skeptic import (  # noqa: F401
    _build_cross_examine_user_prompt,
    _build_rule_based_skeptic_output,
    _LLMSkepticOutput,
    _run_skeptic,
    _run_skeptic_cross_examine,
    _run_skeptic_cross_examine_with_llm,
)
from .pipeline import _execute_pipeline, recover_zombie_runs  # noqa: F401
from .prompts import (  # noqa: F401
    _CROSS_EXAMINE_SYSTEM_PROMPT,
    _META_CONTROLLER_SYSTEM_PROMPT,
    PROMPT_VERSIONS,
)

router = APIRouter(prefix="/agent", tags=["agent"])

logger = logging.getLogger("agent-service")

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
    try:
        await db.flush()
    except IntegrityError as exc:
        # 并发竞态：查重 SELECT 之后另一请求已插入活跃 run，被
        # uq_agentruns_one_active_per_case 部分唯一索引（迁移 0005）拦截 ——
        # 回滚后转成与上方应用层查重一致的 409，而非裸 500
        await db.rollback()
        raise ConflictError(
            f"案例 {body.case_id} 已有进行中的流水线，请等待完成、暂停或取消后再启动"
        ) from exc

    await append_audit_log(
        db,
        operator=actor,
        action="AGENT_PIPELINE_START",
        target=body.case_id,
        detail=f"启动 Agent 流水线 (run_id={run_id}, flags 快照见 run.agent_states.meta_controller)",
        ip_address=request.client.host if request.client else "unknown",
    )

    # 快路径：请求进程内直接后台执行（worker 认领失败即跳过，pending 兜底由
    # agent-service 的 DB 轮询 worker 接管 —— 进程死亡后 run 不再丢失）
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
    # resume 已把状态置为 running（非 pending），无需再走认领 —— claimed=True 直通
    background_tasks.add_task(_execute_pipeline, run.case_id, run_id, run.triggered_by, claimed=True)
    return {"status": "running", "run_id": run_id}
