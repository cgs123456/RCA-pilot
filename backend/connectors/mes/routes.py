"""MES Mock Connector — 生产执行系统只读 API"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query

from shared.connector_auth import require_connector_auth

router = APIRouter(prefix="/mes", tags=["mes-connector"], dependencies=[Depends(require_connector_auth)])


# ============================================================
#  Mock 数据 — 模拟 MES 数据库（批次/清洁/CCTV）
# ============================================================
BATCHES = {
    "BN-2025-08": {
        "batch_id": "BN-2025-08",
        "product": "注射液 5mL",
        "strength": "10mg/mL",
        "line": "灌装线 FS-301",
        "area": "B 级区 Room-3B",
        "start_at": "2025-08-12T08:00:00+08:00",
        "end_at": "2025-08-16T20:00:00+08:00",
        "status": "released",
        "operator": "EMP-2187",
        "deviation_refs": ["DEV-2025-08-147"],
    },
}

CLEANING_RECORDS: list[Any] = [
    {
        "record_id": "WO-20250814-0401",
        "work_order": "WO-20250814-0401",
        "room": "Room-3B",
        "asset": "灌装机-FS-301",
        "cleaning_type": "级别变更清洁",
        "performed_at": "2025-08-14T22:00:00+08:00",
        "completed_at": "2025-08-15T02:30:00+08:00",
        "operator": "EMP-2187",
        "verified_by": "QA-EMP-3301",
        "status": "released",
        "deviation_refs": ["DEV-2025-08-147"],
    },
    {
        "record_id": "WO-20250812-0301",
        "work_order": "WO-20250812-0301",
        "room": "Room-3B",
        "asset": "灌装机-FS-301",
        "cleaning_type": "批次后清洁",
        "performed_at": "2025-08-12T20:00:00+08:00",
        "completed_at": "2025-08-12T23:00:00+08:00",
        "operator": "EMP-2187",
        "verified_by": "QA-EMP-3301",
        "status": "released",
    },
]

CCTV_ANOMALIES = [
    {
        "event_id": "E-CCTV-01",
        "ts": "2025-08-12T14:23:00+08:00",
        "camera": "CAM-R3B-GOWN-01",
        "location": "Room-3B 更衣通道",
        "employee_id": "EMP-2187",
        "anomaly_type": "inner_glove_not_covered",
        "description": "内层手套未完全覆盖至袖口",
        "severity": "minor",
        "review_status": "missed",
    },
    {
        "event_id": "E-CCTV-02",
        "ts": "2025-08-13T14:18:00+08:00",
        "camera": "CAM-R3B-GOWN-01",
        "location": "Room-3B 更衣通道",
        "employee_id": "EMP-2187",
        "anomaly_type": "inner_glove_not_covered",
        "description": "内层手套未完全覆盖至袖口",
        "severity": "minor",
        "review_status": "missed",
    },
    {
        "event_id": "E-CCTV-03",
        "ts": "2025-08-14T14:30:00+08:00",
        "camera": "CAM-R3B-GOWN-01",
        "location": "Room-3B 更衣通道",
        "employee_id": "EMP-2187",
        "anomaly_type": "inner_glove_not_covered",
        "description": "内层手套未完全覆盖至袖口",
        "severity": "minor",
        "review_status": "missed",
    },
    {
        "event_id": "E-CCTV-04",
        "ts": "2025-08-15T14:15:00+08:00",
        "camera": "CAM-R3B-GOWN-01",
        "location": "Room-3B 更衣通道",
        "employee_id": "EMP-2187",
        "anomaly_type": "inner_glove_not_covered",
        "description": "内层手套未完全覆盖至袖口",
        "severity": "minor",
        "review_status": "missed",
    },
]


@router.get("/health", summary="MES Connector 健康检查")
async def health():
    return {
        "status": "healthy",
        "service": "mes-connector",
        "data_freshness": datetime.now(UTC).astimezone().isoformat(),
        "is_readonly": True,
    }


@router.get("/batches/{batch_id}", summary="查询批次信息")
async def get_batch(batch_id: str) -> dict:
    """查询指定批次的生产执行信息

    关键发现：BN-2025-08 (注射液 5mL) 由 EMP-2187 操作，
    对应 Room-3B 沉降菌超标的受影响批次
    """
    batch = BATCHES.get(batch_id)
    if not batch:
        return {
            "status": "not_found",
            "batch_id": batch_id,
            "message": f"批次 {batch_id} 不存在于 MES",
        }
    return {
        "status": "ok",
        "source": "MES",
        "fetched_at": datetime.now(UTC).isoformat(),
        "batch": batch,
    }


@router.get("/cleaning-records", summary="查询清洁记录")
async def list_cleaning_records(
    room: str | None = Query(default=None, description="按房间过滤"),
    asset: str | None = Query(default=None, description="按资产过滤"),
) -> dict:
    """查询清洁记录 — 支持按房间/资产过滤

    关键发现：WO-20250814-0401 (Room-3B 灌装机-FS-301 级别变更清洁)
    由 EMP-2187 执行，对应 BMS AHU-7 HEPA 更换窗口
    """
    items = CLEANING_RECORDS
    if room:
        items = [r for r in items if r["room"] == room]
    if asset:
        items = [r for r in items if r["asset"] == asset]
    return {
        "status": "ok",
        "source": "MES",
        "fetched_at": datetime.now(UTC).isoformat(),
        "total": len(items),
        "items": items,
    }


@router.get("/cctv/anomalies", summary="查询 CCTV 更衣异常事件")
async def list_cctv_anomalies(
    employee_id: str | None = Query(default=None, description="按员工 ID 过滤"),
    anomaly_type: str | None = Query(default=None, description="按异常类型过滤"),
) -> dict:
    """查询 CCTV 更衣异常事件 — 用于偏差调查证据收集

    关键发现：EMP-2187 在 08-12 ~ 08-15 连续 4 次 inner_glove_not_covered
    异常均被 CCTV 系统漏检（review_status=missed），未触发实时拦截
    """
    items = CCTV_ANOMALIES
    if employee_id:
        items = [e for e in items if e["employee_id"] == employee_id]
    if anomaly_type:
        items = [e for e in items if e["anomaly_type"] == anomaly_type]
    return {
        "status": "ok",
        "source": "MES",
        "fetched_at": datetime.now(UTC).isoformat(),
        "total": len(items),
        "items": items,
    }
