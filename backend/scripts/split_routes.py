"""P3-① 一次性拆分脚本：agent_service/routes.py → 六个模块（按当前行号精确切片，正文逐字搬运）

行号基线：拆分前 grep -nE 结构图（含 prompt_version 编辑后的行号）。
"""
from pathlib import Path

BACKEND = Path(r"C:\Users\Administrator\Desktop\RCA\backend")
SRC = BACKEND / "agent_service" / "routes.py"
OUT = BACKEND / "agent_service"
lines = SRC.read_text(encoding="utf-8").splitlines(keepends=True)

def seg(a, b):
    return "".join(lines[a - 1 : b])

HEADER = '''"""{doc}

P3-① 自 agent_service/routes.py 拆出（机械搬运，正文与原文件逐字一致）。
"""
'''

COMMON_IMPORTS = '''from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel as PydBaseModel
from pydantic import Field as PydField
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.audit import append_audit_log
from shared.config import settings
from shared.database import db_session, release_db_connection
from shared.exceptions import ConflictError, NotFoundError, ValidationError
from shared.feature_flag import is_enabled
from shared.llm_client import (
    LLMError,
    LLMResponseError,
    LLMTruncatedError,
    get_debate_client,
    llm_client,
)
from shared.metrics import AGENT_RUNS_TOTAL, AGENT_STAGE_DURATION
from shared.models import AgentRun, AuditLog, Deviation, Evidence, TimelineEvent
from shared.text_guard import fenced as _fence
from shared.text_guard import sanitize_untrusted as _sanitize
'''

bodies = {
    "agents.py": (
        "Agent 公共辅助 + Evidence Hunter + Guardian（P3-① 自 routes.py 拆出）",
        seg(78, 192) + "\n" + seg(1671, 1735),
    ),
    "prompts.py": (
        "Agent 系统提示词与 user prompt 组装（受控资产，见 docs/COU.md 第 5 节）",
        "PROMPT_VERSIONS: dict[str, str] = {\n"
        '    "reasoner": "1.0.0",\n'
        '    "skeptic": "1.0.0",\n'
        '    "drafter": "1.0.0",\n'
        '    "meta_controller": "1.0.0",\n'
        '    "cross_examine": "1.0.0",\n'
        "}\n\n\n"
        + seg(368, 453) + "\n" + seg(684, 766) + "\n"
        + seg(928, 1032) + "\n" + seg(1122, 1227) + "\n" + seg(1289, 1355),
    ),
    "llm_reasoner.py": (
        "Reasoner：5 Whys + 6M 鱼骨图（规则版 / LLM 版，feature flag 灰度）",
        seg(193, 322) + "\n" + seg(329, 367) + "\n" + seg(454, 539),
    ),
    "llm_skeptic.py": (
        "Skeptic：对抗性质询 + 交叉质证（规则版 / LLM 版）",
        seg(540, 683) + "\n" + seg(767, 835) + "\n"
        + seg(1285, 1288) + seg(1356, 1415) + "\n" + seg(1571, 1612),
    ),
    "llm_drafter.py": (
        "Drafter：ICH Q9 调查报告起草（规则版 / LLM 版）",
        seg(836, 927) + "\n" + seg(1033, 1088),
    ),
    "llm_debate.py": (
        "Multi-Agent 博弈：双 Reasoner 辩论 + Meta-Controller 置信度综合",
        seg(1089, 1121) + "\n" + seg(1228, 1284) + "\n"
        + seg(1416, 1570) + "\n" + seg(1613, 1670),
    ),
    "pipeline.py": (
        "流水线编排：阶段执行、审计留痕、指标、僵尸恢复、DB 轮询 worker 取件",
        seg(1736, 2133),
    ),
}

for fname, (doc, body) in bodies.items():
    (OUT / fname).write_text(HEADER.format(doc=doc) + COMMON_IMPORTS + "\n" + body, encoding="utf-8")
    print(f"{fname}: {len(body.splitlines())} body lines")

# ---- 重写 routes.py：头部 1-77 + 新导入 + 端点段 2134-末尾 ----
head = seg(1, 77)
endpoints = seg(2134, len(lines))
new_routes = (
    head
    + "\n"
    + "# P3-① 拆分：Agent 实现移至同包各模块；此处仅保留 HTTP 端点与请求/响应模型\n"
    + "from .agents import (  # noqa: F401\n"
    + "    _case_evidence_ids,\n"
    + "    _filter_hallucinated_evidence,\n"
    + "    _hash_output,\n"
    + "    _run_evidence_hunter,\n"
    + "    _run_guardian,\n"
    + ")\n"
    + "from .llm_reasoner import (\n"
    + "    _LLMReasonerOutput,\n"
    + "    _build_rule_based_reasoner_output,\n"
    + "    _run_reasoner,\n"
    + "    _run_reasoner_with_llm,\n"
    + ")\n"
    + "from .llm_skeptic import (\n"
    + "    _run_skeptic,\n"
    + "    _run_skeptic_cross_examine,\n"
    + ")\n"
    + "from .llm_drafter import _run_drafter\n"
    + "from .llm_debate import (\n"
    + "    _run_drafter_debate,\n"
    + "    _run_meta_controller_synthesis,\n"
    + "    _run_reasoner_debate,\n"
    + ")\n"
    + "from .pipeline import (\n"
    + "    _execute_pipeline,\n"
    + "    recover_zombie_runs,\n"
    + ")\n"
    + "from .prompts import PROMPT_VERSIONS  # noqa: F401\n"
    + "\n"
    + endpoints
)
# 修正：AGENT_PIPELINE/AGENT_LABELS 仍在头部 1-77（属常量，保留在 routes），
# 但 pipeline.py 也需要 —— 头部段中把它们复制给 pipeline 模块。
pipeline_consts = seg(53, 76)  # AGENT_PIPELINE + AGENT_LABELS + ACTIVE_RUN_STATUSES + ZOMBIE_*
pdoc, pbody = bodies["pipeline.py"]
(OUT / "pipeline.py").write_text(
    HEADER.format(doc=pdoc) + COMMON_IMPORTS + "\n" + pipeline_consts + "\n\n" + seg(1736, 2133),
    encoding="utf-8",
)
(OUT / "routes.py").write_text(new_routes, encoding="utf-8")
print("routes.py rewritten:", len(new_routes.splitlines()), "lines")
