"""P2-⑧ 站内协作通知端点 — 最小闭环

GET  /notifications               自己的通知（新→旧）+ 未读数
POST /notifications/{id}/read     标记单条已读（仅本人）
POST /notifications/read-all      全部已读

注意：与本服务其他路由模块一致，不使用 `from __future__ import annotations`
（slowapi 对限流装饰器端点做注解解析）；限流走 @api_limit/@write_limit 装饰器
（与 routes.py/audit/evidence 同风格）。不得改为 `Depends(api_limit)` 形式——
slowapi 限流器是装饰器而非依赖，FastAPI 会把装饰器签名的 `func` 形参解析成
必填 query 参数，所有请求 422。
"""
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth_middleware import AuthContext, get_db_from_auth, require
from shared.exceptions import NotFoundError
from shared.models import Notification
from shared.rate_limit import api_limit, write_limit
from shared.schemas import NotificationListOut, NotificationOut

router = APIRouter(tags=["notifications"])


@router.get("/notifications", response_model=NotificationListOut)
@api_limit
async def list_notifications(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db_from_auth),
    auth: AuthContext = Depends(require("case:read")),
) -> NotificationListOut:
    """当前用户的通知列表（新→旧）与未读数 — 只看得到自己的"""
    total_unread = (
        await db.execute(
            select(func.count())
            .select_from(Notification)
            .where(Notification.user_id == auth.user_id, Notification.read_at.is_(None))
        )
    ).scalar_one()
    rows = (
        await db.execute(
            select(Notification)
            .where(Notification.user_id == auth.user_id)
            .order_by(Notification.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return NotificationListOut(
        items=[NotificationOut.model_validate(n) for n in rows],
        unread=total_unread,
    )


@router.post("/notifications/{notification_id}/read", response_model=NotificationOut)
@write_limit
async def mark_notification_read(
    request: Request,
    notification_id: str,
    db: AsyncSession = Depends(get_db_from_auth),
    auth: AuthContext = Depends(require("case:read")),
) -> Notification:
    """标记单条通知已读 — 只能操作本人的通知"""
    notification = (
        await db.execute(
            select(Notification)
            .where(Notification.id == notification_id, Notification.user_id == auth.user_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not notification:
        raise NotFoundError(f"通知 {notification_id} 不存在")
    if notification.read_at is None:
        notification.read_at = datetime.now(UTC)
        await db.commit()
    await db.refresh(notification)
    return notification


@router.post("/notifications/read-all")
@write_limit
async def mark_all_notifications_read(
    request: Request,
    db: AsyncSession = Depends(get_db_from_auth),
    auth: AuthContext = Depends(require("case:read")),
) -> dict:
    """全部已读（幂等）"""
    result = await db.execute(
        update(Notification)
        .where(Notification.user_id == auth.user_id, Notification.read_at.is_(None))
        .values(read_at=datetime.now(UTC))
    )
    await db.commit()
    return {"updated": result.rowcount or 0}
