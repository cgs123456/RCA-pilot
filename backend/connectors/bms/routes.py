"""BMS Mock Connector — 楼宇管理系统 HVAC 只读 API"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query

from shared.connector_auth import require_connector_auth

router = APIRouter(prefix="/bms", tags=["bms-connector"], dependencies=[Depends(require_connector_auth)])


# ============================================================
#  Mock 数据 — 模拟 BMS 数据库（AHU HVAC 时序）
# ============================================================
AHUS: dict[str, Any] = {
    "AHU-7": {
        "ahu_id": "AHU-7",
        "area": "B 级区 Room-3B",
        "serves": "灌装线 FS-301",
        "filter": "HEPA H14",
        "filter_last_replaced": "2025-08-14",
        "status": "warning",
        "params": {
            "delta_p_pa": {"alert": 450, "action": 480, "unit": "Pa"},
            "temperature_c": {"alert": 22.0, "action": 24.0, "unit": "°C"},
            "humidity_pct": {"alert": 55, "action": 60, "unit": "%RH"},
        },
        "history": [
            {"ts": "2025-08-10T06:00:00+08:00", "delta_p_pa": 380, "temperature_c": 20.5, "humidity_pct": 48},
            {"ts": "2025-08-11T06:00:00+08:00", "delta_p_pa": 420, "temperature_c": 20.7, "humidity_pct": 49},
            {"ts": "2025-08-12T06:00:00+08:00", "delta_p_pa": 470, "temperature_c": 21.0, "humidity_pct": 50},
            {"ts": "2025-08-13T06:00:00+08:00", "delta_p_pa": 465, "temperature_c": 21.2, "humidity_pct": 51},
            {"ts": "2025-08-14T06:00:00+08:00", "delta_p_pa": 400, "temperature_c": 20.6, "humidity_pct": 48, "note": "HEPA 更换"},
            {"ts": "2025-08-15T06:00:00+08:00", "delta_p_pa": 410, "temperature_c": 20.8, "humidity_pct": 49},
            {"ts": "2025-08-16T06:00:00+08:00", "delta_p_pa": 435, "temperature_c": 21.1, "humidity_pct": 50},
        ],
        "anomalies": [
            {
                "event_id": "E-BMS-01",
                "ts": "2025-08-12T06:00:00+08:00",
                "param": "delta_p_pa",
                "value": 470,
                "threshold": 450,
                "level": "alert",
                "msg": "Room-3B AHU-7 ΔP 超警戒阈值",
            },
            {
                "event_id": "E-BMS-02",
                "ts": "2025-08-13T06:00:00+08:00",
                "param": "delta_p_pa",
                "value": 465,
                "threshold": 450,
                "level": "alert",
                "msg": "Room-3B AHU-7 ΔP 持续偏高，疑似 HEPA 堵塞",
            },
            {
                "event_id": "E-BMS-03",
                "ts": "2025-08-14T06:00:00+08:00",
                "param": "filter",
                "action": "replaced",
                "msg": "Room-3B AHU-7 HEPA H14 更换，ΔP 恢复 400 Pa",
            },
            {
                "event_id": "E-BMS-04",
                "ts": "2025-08-16T06:00:00+08:00",
                "param": "delta_p_pa",
                "value": 435,
                "threshold": 450,
                "level": "warning",
                "msg": "Room-3B AHU-7 ΔP 回升趋势，建议持续观察",
            },
        ],
    },
    "AHU-8": {
        "ahu_id": "AHU-8",
        "area": "B 级区 Room-3C",
        "serves": "灌装线 FS-302",
        "filter": "HEPA H14",
        "filter_last_replaced": "2025-05-20",
        "status": "normal",
        "params": {
            "delta_p_pa": {"alert": 450, "action": 480, "unit": "Pa"},
            "temperature_c": {"alert": 22.0, "action": 24.0, "unit": "°C"},
            "humidity_pct": {"alert": 55, "action": 60, "unit": "%RH"},
        },
        "history": [
            {"ts": "2025-08-10T06:00:00+08:00", "delta_p_pa": 390, "temperature_c": 20.4, "humidity_pct": 47},
            {"ts": "2025-08-16T06:00:00+08:00", "delta_p_pa": 395, "temperature_c": 20.5, "humidity_pct": 48},
        ],
        "anomalies": [],
    },
}


@router.get("/health", summary="BMS Connector 健康检查")
async def health():
    return {
        "status": "healthy",
        "service": "bms-connector",
        "data_freshness": datetime.now(UTC).astimezone().isoformat(),
        "is_readonly": True,
    }


@router.get("/ahus", summary="查询所有 AHU 摘要")
async def list_ahus() -> dict:
    """查询所有空气处理机组（AHU）摘要列表"""
    return {
        "status": "ok",
        "source": "BMS",
        "fetched_at": datetime.now(UTC).isoformat(),
        "total": len(AHUS),
        "items": [
            {
                "ahu_id": a["ahu_id"],
                "area": a["area"],
                "serves": a["serves"],
                "filter": a["filter"],
                "filter_last_replaced": a["filter_last_replaced"],
                "status": a["status"],
            }
            for a in AHUS.values()
        ],
    }


@router.get("/ahus/{ahu_id}/history", summary="查询 AHU 历史时序数据")
async def get_ahu_history(
    ahu_id: str,
    param: str | None = Query(default=None, description="参数名：delta_p_pa / temperature_c / humidity_pct"),
) -> dict:
    """查询指定 AHU 的参数历史时序数据

    关键发现：AHU-7 (Room-3B) ΔP 在 08-12 超警戒值 470 Pa，
    08-14 更换 HEPA 后恢复，但 08-16 有回升趋势（435 Pa）
    """
    ahu = AHUS.get(ahu_id)
    if not ahu:
        return {
            "status": "not_found",
            "ahu_id": ahu_id,
            "message": f"AHU {ahu_id} 不存在于 BMS",
        }
    history = ahu["history"]
    if param:
        history = [{"ts": h["ts"], param: h.get(param)} for h in history]
    return {
        "status": "ok",
        "source": "BMS",
        "fetched_at": datetime.now(UTC).isoformat(),
        "ahu_id": ahu_id,
        "area": ahu["area"],
        "params": ahu["params"],
        "history": history,
    }


@router.get("/anomalies", summary="查询 BMS 异常/告警事件")
async def list_anomalies(
    level: str | None = Query(default=None, description="过滤级别：warning / alert / action"),
) -> dict:
    """查询 BMS 所有异常/告警事件 — 用于偏差调查证据收集

    关键发现：AHU-7 ΔP 告警事件链 (E-BMS-01 ~ E-BMS-04)，
    对应 Room-3B 沉降菌超标时间窗口
    """
    items = []
    for ahu in AHUS.values():
        for ev in ahu.get("anomalies", []):
            if level and ev.get("level") != level:
                continue
            items.append({"ahu_id": ahu["ahu_id"], "area": ahu["area"], **ev})
    return {
        "status": "ok",
        "source": "BMS",
        "fetched_at": datetime.now(UTC).isoformat(),
        "total": len(items),
        "items": items,
    }
