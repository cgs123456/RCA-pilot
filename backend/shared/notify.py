"""P2-⑧ 站内协作通知 — 共享写入助手

最小闭环的事件集合（type）：
    - CASE_ASSIGNED  案例被指派给 QA 审核人
    - GATE_READY     流水线停在 HITL 关卡等待人工决策
    - CASE_CLOSED    案例完成双人电子签名结案
    - CAPA_CREATED   案例下新建了 CAPA

读取端点在 case_service/notification_routes.py；通知只增不改（已读用 read_at 标记）。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import Notification

NOTIFICATION_TYPES = ("CASE_ASSIGNED", "GATE_READY", "CASE_CLOSED", "CAPA_CREATED")


async def create_notification(
    db: AsyncSession,
    user_id: str | None,
    notification_type: str,
    case_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> Notification | None:
    """写入一条通知；user_id 为空（未指派）时静默跳过。

    与审计不同，通知是易失的协作辅助数据：失败不阻断业务主流程。
    """
    if not user_id:
        return None
    notification = Notification(
        id=f"NTF-{uuid.uuid4().hex[:12]}",
        user_id=user_id,
        case_id=case_id,
        type=notification_type,
        payload=payload or {},
        created_at=datetime.now(UTC),
    )
    db.add(notification)
    return notification
