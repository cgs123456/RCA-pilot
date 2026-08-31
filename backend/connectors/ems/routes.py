"""EMS Mock Connector — 环境监测系统只读 API"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query

from shared.connector_auth import require_connector_auth

router = APIRouter(prefix="/ems", tags=["ems-connector"], dependencies=[Depends(require_connector_auth)])


# ============================================================
#  Mock 数据 — 模拟 EMS 数据库（沉降菌/浮游菌采样）
# ============================================================
MONITORING_POINTS: dict[str, Any] = {
    "Room-3B-Act-12": {
        "point_id": "Room-3B-Act-12",
        "area": "B 级区 Room-3B",
        "type": "settling_plate",
        "method": "沉降菌法（4 小时暴露）",
        "alert_threshold": 5,
        "action_threshold": 15,
        "unit": "CFU",
        "status": "action",
        "history": [
            {"ts": "2025-08-12T14:00:00+08:00", "cfu": 3, "status": "normal"},
            {"ts": "2025-08-13T14:00:00+08:00", "cfu": 8, "status": "alert"},
            {"ts": "2025-08-14T14:00:00+08:00", "cfu": 12, "status": "alert"},
            {"ts": "2025-08-15T14:00:00+08:00", "cfu": 15, "status": "action"},
            {"ts": "2025-08-16T14:00:00+08:00", "cfu": 22, "status": "action"},
        ],
        "exceedances": [
            {
                "event_id": "E-EMS-01",
                "ts": "2025-08-13T14:00:00+08:00",
                "cfu": 8,
                "level": "alert",
                "msg": "Room-3B 沉降菌超警戒（5 CFU）",
            },
            {
                "event_id": "E-EMS-02",
                "ts": "2025-08-14T14:00:00+08:00",
                "cfu": 12,
                "level": "alert",
                "msg": "Room-3B 沉降菌持续超警戒",
            },
            {
                "event_id": "E-EMS-03",
                "ts": "2025-08-15T14:00:00+08:00",
                "cfu": 15,
                "level": "action",
                "msg": "Room-3B 沉降菌达行动阈值（15 CFU）",
            },
            {
                "event_id": "E-EMS-04",
                "ts": "2025-08-16T14:00:00+08:00",
                "cfu": 22,
                "level": "action",
                "msg": "Room-3B 沉降菌超行动阈值，触发偏差 DEV-2025-08-147",
            },
        ],
    },
    "Room-3B-Act-13": {
        "point_id": "Room-3B-Act-13",
        "area": "B 级区 Room-3B",
        "type": "active_air",
        "method": "浮游菌法（1 m³ 采样）",
        "alert_threshold": 10,
        "action_threshold": 50,
        "unit": "CFU/m³",
        "status": "normal",
        "history": [
            {"ts": "2025-08-12T14:00:00+08:00", "cfu": 3, "status": "normal"},
            {"ts": "2025-08-16T14:00:00+08:00", "cfu": 5, "status": "normal"},
        ],
        "exceedances": [],
    },
}


@router.get("/health", summary="EMS Connector 健康检查")
async def health():
    return {
        "status": "healthy",
        "service": "ems-connector",
        "data_freshness": datetime.now(UTC).astimezone().isoformat(),
        "is_readonly": True,
    }


@router.get("/points", summary="查询所有环境监测点")
async def list_points() -> dict:
    """查询所有环境监测点摘要列表"""
    return {
        "status": "ok",
        "source": "EMS",
        "fetched_at": datetime.now(UTC).isoformat(),
        "total": len(MONITORING_POINTS),
        "items": [
            {
                "point_id": p["point_id"],
                "area": p["area"],
                "type": p["type"],
                "method": p["method"],
                "alert_threshold": p["alert_threshold"],
                "action_threshold": p["action_threshold"],
                "status": p["status"],
            }
            for p in MONITORING_POINTS.values()
        ],
    }


@router.get("/points/{point_id}/history", summary="查询监测点历史采样数据")
async def get_point_history(point_id: str) -> dict:
    """查询指定监测点的历史采样数据

    关键发现：Room-3B-Act-12 沉降菌从 08-12 起持续上升，
    08-15 达行动阈值 15 CFU，08-16 升至 22 CFU 触发偏差
    """
    point = MONITORING_POINTS.get(point_id)
    if not point:
        return {
            "status": "not_found",
            "point_id": point_id,
            "message": f"监测点 {point_id} 不存在于 EMS",
        }
    return {
        "status": "ok",
        "source": "EMS",
        "fetched_at": datetime.now(UTC).isoformat(),
        **point,
    }


@router.get("/exceedances", summary="查询环境监测超限事件")
async def list_exceedances(
    status: str | None = Query(default=None, description="过滤状态：alert / action"),
) -> dict:
    """查询所有环境监测超限事件 — 用于偏差调查证据收集

    关键发现：Room-3B 沉降菌超限事件链 (E-EMS-01 ~ E-EMS-04)，
    对应案例 DEV-2025-08-147 的核心证据
    """
    items = []
    for point in MONITORING_POINTS.values():
        for ev in point.get("exceedances", []):
            if status and ev.get("level") != status:
                continue
            items.append({"point_id": point["point_id"], "area": point["area"], **ev})
    return {
        "status": "ok",
        "source": "EMS",
        "fetched_at": datetime.now(UTC).isoformat(),
        "total": len(items),
        "items": items,
    }
