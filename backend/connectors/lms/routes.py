"""LMS Mock Connector — 培训管理系统只读 API"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query

from shared.connector_auth import require_connector_auth

router = APIRouter(prefix="/lms", tags=["lms-connector"], dependencies=[Depends(require_connector_auth)])


# ============================================================
#  Mock 数据 — 模拟 LMS 数据库
# ============================================================
EMPLOYEES: dict[str, Any] = {
    "EMP-2156": {
        "employee_id": "EMP-2156",
        "name": "EMP-2156",
        "role": "清洁操作员",
        "dept": "生产",
        "shift": "晚班",
        "certifications": [
            {
                "cert_id": "CERT-GOWN-B-2156",
                "cert_name": "B 级区更衣资质",
                "valid_from": "2024-12-16",
                "valid_until": "2025-12-15",
                "status": "valid",
                "issued_by": "QA-Training",
            },
            {
                "cert_id": "CERT-SAFETY-2156",
                "cert_name": "实验室安全培训",
                "valid_from": "2024-06-01",
                "valid_until": "2025-12-01",
                "status": "valid",
            },
        ],
        "training_records": [
            {"course": "GMP 基础", "completed_at": "2025-01-15", "score": 95},
            {"course": "无菌操作", "completed_at": "2025-03-20", "score": 88},
            {"course": "更衣认证复训", "completed_at": "2024-12-16", "score": 92},
        ],
    },
    "EMP-2187": {
        "employee_id": "EMP-2187",
        "name": "EMP-2187",
        "role": "清洁操作员",
        "dept": "生产",
        "shift": "晚班",
        "certifications": [
            {
                "cert_id": "CERT-GOWN-B-2187",
                "cert_name": "B 级区更衣资质",
                "valid_from": "2024-08-11",
                "valid_until": "2025-08-10",
                "status": "expired",
                "days_expired": 2,
                "issued_by": "QA-Training",
            },
        ],
        "training_records": [
            {"course": "GMP 基础", "completed_at": "2024-08-10", "score": 85},
            {"course": "无菌操作", "completed_at": "2024-08-10", "score": 78},
            {"course": "更衣认证复训", "completed_at": "2024-08-10", "score": 80},
        ],
        "alert_log": [],  # 设计意图：此处故意为空数组，证明 LMS 与排班系统未集成，资质逾期未触发系统告警 —— 这是本案例的核心缺陷证据（见 E-LMS-03）
    },
    "EMP-2191": {
        "employee_id": "EMP-2191",
        "name": "EMP-2191",
        "role": "清洁操作员",
        "dept": "生产",
        "shift": "白班",
        "certifications": [
            {
                "cert_id": "CERT-GOWN-B-2191",
                "cert_name": "B 级区更衣资质",
                "valid_from": "2024-11-02",
                "valid_until": "2025-11-01",
                "status": "valid",
            },
        ],
        "training_records": [
            {"course": "GMP 基础", "completed_at": "2024-11-01", "score": 90},
        ],
    },
}


@router.get("/health", summary="LMS Connector 健康检查")
async def health():
    return {
        "status": "healthy",
        "service": "lms-connector",
        "data_freshness": datetime.now(UTC).astimezone().isoformat(),
        "is_readonly": True,
    }


@router.get("/cert-status", summary="查询员工资质状态")
async def get_cert_status(
    employee_id: str | None = Query(default=None, description="员工 ID"),
) -> dict:
    """查询员工资质状态 — 返回资质、培训记录、告警日志

    关键发现：EMP-2187 资质已逾期 2 天但 LMS 未触发告警
    """
    if employee_id:
        emp = EMPLOYEES.get(employee_id)
        if not emp:
            return {
                "status": "not_found",
                "employee_id": employee_id,
                "message": f"员工 {employee_id} 不存在于 LMS",
            }
        return {
            "status": "ok",
            "source": "LMS",
            "fetched_at": datetime.now(UTC).isoformat(),
            "employee": emp,
        }

    # 返回所有员工摘要
    return {
        "status": "ok",
        "source": "LMS",
        "fetched_at": datetime.now(UTC).isoformat(),
        "total": len(EMPLOYEES),
        "employees": [
            {
                "employee_id": eid,
                "name": emp["name"],
                "role": emp["role"],
                "cert_count": len(emp.get("certifications", [])),
                "expired_count": sum(1 for c in emp.get("certifications", []) if c.get("status") == "expired"),
            }
            for eid, emp in EMPLOYEES.items()
        ],
    }


@router.get("/certifications/expired", summary="查询所有过期资质")
async def list_expired_certs() -> dict:
    """查询所有已过期的资质 — 用于偏差调查证据收集"""
    expired = []
    for eid, emp in EMPLOYEES.items():
        for cert in emp.get("certifications", []):
            if cert.get("status") == "expired":
                expired.append({
                    "employee_id": eid,
                    "employee_name": emp["name"],
                    "dept": emp["dept"],
                    "shift": emp["shift"],
                    **cert,
                })
    return {
        "status": "ok",
        "source": "LMS",
        "fetched_at": datetime.now(UTC).isoformat(),
        "total_expired": len(expired),
        "items": expired,
    }


@router.get("/alert-log", summary="查询 LMS 告警日志")
async def get_alert_log(
    employee_id: str | None = Query(default=None),
) -> dict:
    """查询 LMS 告警日志

    数据驱动判定：Mock 告警日志表恒为空 —— 任何资质已逾期的员工都构成
    "应触发告警但未触发" 异常发现（不再特判特定员工）。
    """
    if employee_id:
        emp = EMPLOYEES.get(employee_id)
        expired = [
            c for c in (emp.get("certifications", []) if emp else [])
            if c.get("status") == "expired"
        ]
        if expired:
            return {
                "status": "ok",
                "source": "LMS",
                "employee_id": employee_id,
                "expired_certs": [c["cert_id"] for c in expired],
                "actual_alerts": [],
                "finding": "LMS 与排班系统无资质门控集成 — 资质逾期未触发告警",
                "is_anomaly": True,
            }

    return {
        "status": "ok",
        "source": "LMS",
        "total_alerts": 0,
        "items": [],
    }


@router.get("/employees/{employee_id}/training-records", summary="员工培训记录")
async def get_training_records(employee_id: str) -> dict:
    """查询员工完整培训记录"""
    emp = EMPLOYEES.get(employee_id)
    if not emp:
        return {
            "status": "not_found",
            "employee_id": employee_id,
        }
    return {
        "status": "ok",
        "source": "LMS",
        "employee_id": employee_id,
        "employee_name": emp["name"],
        "records": emp.get("training_records", []),
    }
