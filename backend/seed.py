"""RCA Pilot 数据库种子加载器

从 Demo mockData.js 移植：8 用户 + 6 源系统 + 4 案例（含证据/时间线/5Whys/Skeptic）
用法：
    python -m backend.seed
    或
    python backend/seed.py
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import asyncpg

from shared.config import settings


# ============================================================
#  数据定义
# ============================================================

USERS = [
    {"id": "U-LC", "email": "lisa.chen@biopharma.com", "name": "Lisa Chen", "role": "investigator", "dept": "微生物", "title": "调查员", "avatar": "LC", "color": "brand", "shift": "晚班", "cert": "微生物学博士, 5年经验", "password": "rca-demo-2025"},
    {"id": "U-JW", "email": "james.wilson@biopharma.com", "name": "James Wilson", "role": "qa_reviewer", "dept": "质量保证", "title": "QA 审核员", "avatar": "JW", "color": "success", "shift": "—", "cert": "ASQ CQE, 12年QA经验", "password": "rca-demo-2025"},
    {"id": "U-RK", "email": "robert.kim@biopharma.com", "name": "Robert Kim", "role": "ops_manager", "dept": "生产运营", "title": "运营经理", "avatar": "RK", "color": "warning", "shift": "—", "cert": "工程管理硕士, 15年产线", "password": "rca-demo-2025"},
    {"id": "U-SM", "email": "sarah.martinez@biopharma.com", "name": "Sarah Martinez", "role": "investigator", "dept": "化学分析", "title": "调查员", "avatar": "SM", "color": "brand", "shift": "白班", "cert": "分析化学硕士", "password": "rca-demo-2025"},
    {"id": "U-DL", "email": "david.liu@biopharma.com", "name": "David Liu", "role": "qa_reviewer", "dept": "质量保证", "title": "QA 审核员", "avatar": "DL", "color": "success", "shift": "—", "cert": "药学博士", "password": "rca-demo-2025"},
    {"id": "U-ADMIN", "email": "admin@biopharma.com", "name": "System Admin", "role": "admin", "dept": "IT", "title": "系统管理员", "avatar": "AD", "color": "danger", "shift": "—", "cert": "管理员", "password": "rca-admin-2025"},
    {"id": "U-EMP-2156", "email": "op2156@biopharma.com", "name": "EMP-2156", "role": "operator", "dept": "生产", "title": "清洁操作员", "avatar": "21", "color": "ink", "shift": "晚班", "cert": "更衣资质有效至 2025-12-15", "password": "operator-2025"},
    {"id": "U-EMP-2187", "email": "op2187@biopharma.com", "name": "EMP-2187", "role": "operator", "dept": "生产", "title": "清洁操作员", "avatar": "21", "color": "danger", "shift": "晚班", "cert": "更衣资质已逾期 (2025-08-10)", "password": "operator-2025"},
]

SOURCE_SYSTEMS = [
    {"code": "LIMS", "label": "LIMS — 实验室信息管理", "icon": "flask-conical", "color": "src-lims", "description": "检验数据、菌种鉴定、批次结果", "health_status": "healthy"},
    {"code": "MES", "label": "MES — 制造执行系统", "icon": "clipboard-list", "color": "src-mes", "description": "批记录、工艺参数、电子签名", "health_status": "healthy"},
    {"code": "EMS", "label": "EMS — 环境监控系统", "icon": "thermometer", "color": "src-ems", "description": "沉降菌、尘埃粒子、温湿度", "health_status": "healthy"},
    {"code": "CMMS", "label": "CMMS — 设备维护管理", "icon": "wrench", "color": "src-cmms", "description": "工单、预防维护、紧急维修", "health_status": "degraded"},
    {"code": "LMS", "label": "LMS — 培训管理系统", "icon": "graduation-cap", "color": "src-lms", "description": "资质、培训记录、更衣认证", "health_status": "healthy"},
    {"code": "BMS", "label": "BMS — 楼宇管理系统", "icon": "wind", "color": "src-bms", "description": "HVAC、差压、温湿度、风量", "health_status": "healthy"},
]


def sha256_hex(data: str) -> str:
    return "sha256:" + hashlib.sha256(data.encode("utf-8")).hexdigest()


def evidence_hash(raw: dict) -> str:
    """计算证据 raw_data 的 hash"""
    canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True)
    return sha256_hex(canonical)


# ============================================================
#  CASE A — B 级背景微生物污染（主案例）
# ============================================================
CASE_A = {
    "id": "DEV-2025-08-147",
    "title": "B 级背景细胞培养区连续 5 天检出 Micrococcus luteus 超标",
    "description": "连续 5 天 (8/12–8/16) Room-3B-Act-12 沉降菌计数呈上升趋势，8/16 突破行动线 (15 CFU/孔)，达 22 CFU/孔。PCR 鉴定为 Micrococcus luteus，提示人员来源。",
    "severity": "major",
    "status": "investigating",
    "case_type": "EM + HVAC + 培训",
    "complexity": 3,
    "location": "Suite 3, Room-3B-Act-12",
    "area": "B 级背景 · 细胞培养区",
    "shift": "晚班 (18:00–06:00)",
    "investigator_id": "U-LC",
    "qa_reviewer_id": "U-JW",
    "discovered_at": "2025-08-17T06:00:00+08:00",
    "deadline": "2025-08-31T23:59:59+08:00",
    "organism": "Micrococcus luteus",
    "organism_source": "人体皮肤来源菌",
    "product_impact": "相关批次 AB-2025-218 ~ AB-2025-223 待评估",
    "immediate_action": "1) 受影响批次 (AB-218~223) 暂停放行;\n2) Room-3B-Act-12 增加采样频率至 4h/次;\n3) EMP-2187 暂停 B 级区作业;\n4) AHU-7 预过滤器紧急更换。",
    "progress": 68,
    "hitl_gates": {"gate1": "done", "gate2": "done", "gate3": "pending", "gate4": "pending", "gate5": "pending"},
    "meta": {"affectedBatches": 6, "roomsInvolved": 1, "personnelInvolved": 2},
    "evidence": [
        {"id": "E-EMS-01", "source_system": "EMS", "collected_at": "2025-08-12T20:00:00+08:00", "confidence": 4, "summary": "Room-3B-Act-12 沉降菌 3 CFU/孔 — 正常基线", "raw": {"date": "2025-08-12", "time": "20:00", "point": "Room-3B-Act-12", "cfu": 3, "alert": 5, "action": 15, "status": "normal"}},
        {"id": "E-EMS-02", "source_system": "EMS", "collected_at": "2025-08-13T21:00:00+08:00", "confidence": 4, "summary": "Room-3B-Act-12 沉降菌 8 CFU/孔 — 超过警戒线", "raw": {"date": "2025-08-13", "time": "21:00", "point": "Room-3B-Act-12", "cfu": 8, "alert": 5, "action": 15, "status": "alert"}},
        {"id": "E-EMS-03", "source_system": "EMS", "collected_at": "2025-08-14T19:30:00+08:00", "confidence": 4, "summary": "Room-3B-Act-12 沉降菌 12 CFU/孔 — 接近行动线", "raw": {"date": "2025-08-14", "time": "19:30", "point": "Room-3B-Act-12", "cfu": 12, "alert": 5, "action": 15, "status": "alert"}},
        {"id": "E-EMS-04", "source_system": "EMS", "collected_at": "2025-08-15T21:00:00+08:00", "confidence": 4, "summary": "Room-3B-Act-12 沉降菌 15 CFU/孔 — 达到行动线", "raw": {"date": "2025-08-15", "time": "21:00", "point": "Room-3B-Act-12", "cfu": 15, "alert": 5, "action": 15, "status": "action"}},
        {"id": "E-EMS-05", "source_system": "EMS", "collected_at": "2025-08-16T22:00:00+08:00", "confidence": 4, "summary": "Room-3B-Act-12 沉降菌 22 CFU/孔 — 超过行动线，触发偏差", "raw": {"date": "2025-08-16", "time": "22:00", "point": "Room-3B-Act-12", "cfu": 22, "alert": 5, "action": 15, "status": "action_exceeded"}},
        {"id": "E-LIMS-01", "source_system": "LIMS", "collected_at": "2025-08-17T04:30:00+08:00", "confidence": 4, "summary": "PCR 鉴定结果: Micrococcus luteus — 人体皮肤来源菌", "raw": {"sample": "S-2025-08-16-022", "method": "PCR (16S rRNA)", "organism": "Micrococcus luteus", "identity": "99.2%", "source_hint": "human_skin", "lab": "Micro Lab", "analyst": "U-LC"}},
        {"id": "E-CCTV-01", "source_system": "MES", "collected_at": "2025-08-12T17:22:00+08:00", "confidence": 3, "summary": "CCTV: EMP-2187 第一层手套未完全覆盖袖口", "raw": {"date": "2025-08-12", "time": "17:22", "camera": "CAM-3B-GOWN-02", "person": "EMP-2187", "anomaly": "inner_glove_not_covered", "flagged": True}},
        {"id": "E-CCTV-02", "source_system": "MES", "collected_at": "2025-08-13T17:18:00+08:00", "confidence": 3, "summary": "CCTV: EMP-2187 同类更衣异常 (第 2 次)", "raw": {"date": "2025-08-13", "time": "17:18", "camera": "CAM-3B-GOWN-02", "person": "EMP-2187", "anomaly": "inner_glove_not_covered", "flagged": True}},
        {"id": "E-CCTV-03", "source_system": "MES", "collected_at": "2025-08-14T17:25:00+08:00", "confidence": 3, "summary": "CCTV: EMP-2187 同类更衣异常 (第 3 次)", "raw": {"date": "2025-08-14", "time": "17:25", "camera": "CAM-3B-GOWN-02", "person": "EMP-2187", "anomaly": "inner_glove_not_covered", "flagged": True}},
        {"id": "E-CCTV-04", "source_system": "MES", "collected_at": "2025-08-15T17:20:00+08:00", "confidence": 3, "summary": "CCTV: EMP-2187 更衣异常 + 外层口罩未贴合", "raw": {"date": "2025-08-15", "time": "17:20", "camera": "CAM-3B-GOWN-02", "person": "EMP-2187", "anomaly": ["inner_glove_not_covered","outer_mask_not_sealed"], "flagged": True}},
        {"id": "E-LMS-01", "source_system": "LMS", "collected_at": "2025-08-12T00:00:00+08:00", "confidence": 4, "summary": "EMP-2187 更衣资质已逾期 (有效期 2025-08-10)", "raw": {"employee": "EMP-2187", "role": "清洁操作员", "cert": "B级区更衣资质", "valid_until": "2025-08-10", "status": "expired", "days_expired": 2, "alert_triggered": False}},
        {"id": "E-LMS-02", "source_system": "LMS", "collected_at": "2025-08-12T00:00:00+08:00", "confidence": 4, "summary": "EMP-2156 更衣资质有效 (有效期 2025-12-15)", "raw": {"employee": "EMP-2156", "role": "清洁操作员", "cert": "B级区更衣资质", "valid_until": "2025-12-15", "status": "valid"}},
        {"id": "E-LMS-03", "source_system": "LMS", "collected_at": "2025-08-12T00:00:00+08:00", "confidence": 4, "summary": "LMS 告警日志为空 — 资质逾期未触发系统告警", "raw": {"employee": "EMP-2187", "expected_alert": "资质逾期告警", "actual": "无告警记录", "issue": "LMS 与排班系统未联动"}},
        {"id": "E-BMS-01", "source_system": "BMS", "collected_at": "2025-08-10T06:00:00+08:00", "confidence": 4, "summary": "AHU-7 送风滤芯差压基线 380 Pa", "raw": {"date": "2025-08-10", "time": "06:00", "point": "AHU-7", "param": "ΔP", "value": 380, "unit": "Pa", "threshold": 500, "status": "normal"}},
        {"id": "E-BMS-02", "source_system": "BMS", "collected_at": "2025-08-12T18:30:00+08:00", "confidence": 4, "summary": "AHU-7 差压上升至 420 Pa (↑40)", "raw": {"date": "2025-08-12", "time": "18:30", "point": "AHU-7", "param": "ΔP", "value": 420, "unit": "Pa", "delta": 40, "status": "warning"}},
        {"id": "E-BMS-03", "source_system": "BMS", "collected_at": "2025-08-14T08:00:00+08:00", "confidence": 4, "summary": "AHU-7 差压 470 Pa — 接近维护阈值 500", "raw": {"date": "2025-08-14", "time": "08:00", "point": "AHU-7", "param": "ΔP", "value": 470, "unit": "Pa", "threshold": 500, "status": "warning"}},
        {"id": "E-BMS-04", "source_system": "BMS", "collected_at": "2025-08-14T21:00:00+08:00", "confidence": 4, "summary": "AHU-7 更换预过滤器后差压回落至 400 Pa", "raw": {"date": "2025-08-14", "time": "21:00", "point": "AHU-7", "param": "ΔP", "value": 400, "unit": "Pa", "maintenance": "WO-20250814-0401", "status": "normal"}},
        {"id": "E-BMS-05", "source_system": "BMS", "collected_at": "2025-08-16T23:00:00+08:00", "confidence": 4, "summary": "AHU-7 差压再次上升至 435 Pa", "raw": {"date": "2025-08-16", "time": "23:00", "point": "AHU-7", "param": "ΔP", "value": 435, "unit": "Pa", "status": "warning", "note": "更换后差压再次上升，说明 HVAC 有独立问题"}},
        {"id": "E-CMMS-01", "source_system": "CMMS", "collected_at": "2025-08-11T10:00:00+08:00", "confidence": 3, "summary": "AHU-7 上次预防维护: 2025-05-15 (3 个月前)", "raw": {"work_order": "WO-20250515-0203", "type": "preventive", "asset": "AHU-7", "completed_at": "2025-05-15"}},
        {"id": "E-CMMS-02", "source_system": "CMMS", "collected_at": "2025-08-14T21:00:00+08:00", "confidence": 4, "summary": "AHU-7 预过滤器紧急更换 (WO-20250814-0401)", "raw": {"work_order": "WO-20250814-0401", "type": "corrective", "asset": "AHU-7", "filter": "预过滤 G4", "completed_at": "2025-08-14T21:00:00+08:00"}},
        {"id": "E-CMMS-03", "source_system": "CMMS", "collected_at": "2025-08-15T14:00:00+08:00", "confidence": 4, "summary": "Room-3B 气密门封变形 — 影响压差稳定性", "raw": {"work_order": "WO-20250815-0502", "type": "corrective", "asset": "Room-3B-Door-02", "issue": "门封变形", "status": "open"}},
        {"id": "E-SYS-01", "source_system": "LMS", "collected_at": "2025-08-15T08:00:00+08:00", "confidence": 3, "summary": "LMS 与排班系统架构分析 — 无资质门控 API", "raw": {"finding": "LMS 与 排班系统无直接集成", "current_gating": "人工核查", "risk": "逾期资质员工可能被排入 B 级区"}},
    ],
    "timeline": [
        {"id": "T1", "occurred_at": "2025-08-10T06:00:00+08:00", "source": "BMS", "title": "AHU-7 基线差压", "detail": "ΔP 380 Pa — 正常基值", "severity": "info", "evidence_id": "E-BMS-01", "sort_order": 1},
        {"id": "T2", "occurred_at": "2025-08-11T17:00:00+08:00", "source": "MES", "title": "Room-3B 清洁", "detail": "EMP-2156 执行，Spor-Klenz RTU", "severity": "info", "sort_order": 2},
        {"id": "T3", "occurred_at": "2025-08-12T18:30:00+08:00", "source": "BMS", "title": "AHU-7 差压轻微上升", "detail": "ΔP 420 Pa (↑40)", "severity": "warning", "evidence_id": "E-BMS-02", "sort_order": 3},
        {"id": "T4", "occurred_at": "2025-08-12T20:00:00+08:00", "source": "EMS", "title": "Room-3B-Act-12 首次检出", "detail": "沉降菌 3 CFU/孔 — 正常", "severity": "info", "evidence_id": "E-EMS-01", "sort_order": 4},
        {"id": "T5", "occurred_at": "2025-08-12T17:22:00+08:00", "source": "MES", "title": "EMP-2187 更衣异常", "detail": "CCTV: 第一层手套未完全覆盖袖口", "severity": "warning", "evidence_id": "E-CCTV-01", "sort_order": 5},
        {"id": "T6", "occurred_at": "2025-08-13T21:00:00+08:00", "source": "EMS", "title": "沉降菌超过警戒线", "detail": "8 CFU/孔 (警戒线 5)", "severity": "warning", "evidence_id": "E-EMS-02", "sort_order": 6},
        {"id": "T7", "occurred_at": "2025-08-14T08:00:00+08:00", "source": "BMS", "title": "AHU-7 接近维护阈值", "detail": "ΔP 470 Pa (阈值 500)", "severity": "warning", "evidence_id": "E-BMS-03", "sort_order": 7},
        {"id": "T8", "occurred_at": "2025-08-14T19:30:00+08:00", "source": "EMS", "title": "沉降菌接近行动线", "detail": "12 CFU/孔 (行动线 15)", "severity": "warning", "evidence_id": "E-EMS-03", "sort_order": 8},
        {"id": "T9", "occurred_at": "2025-08-14T21:00:00+08:00", "source": "CMMS", "title": "AHU-7 预过滤器更换", "detail": "WO-20250814-0401 完成，ΔP 回落至 400 Pa", "severity": "info", "evidence_id": "E-CMMS-02", "sort_order": 9},
        {"id": "T10", "occurred_at": "2025-08-15T21:00:00+08:00", "source": "EMS", "title": "沉降菌达到行动线", "detail": "15 CFU/孔 — 触发预警", "severity": "danger", "evidence_id": "E-EMS-04", "sort_order": 10},
        {"id": "T11", "occurred_at": "2025-08-16T22:00:00+08:00", "source": "EMS", "title": "沉降菌超过行动线 ⚠", "detail": "22 CFU/孔 — 触发偏差", "severity": "danger", "evidence_id": "E-EMS-05", "is_anomaly": True, "sort_order": 11},
        {"id": "T12", "occurred_at": "2025-08-16T23:00:00+08:00", "source": "BMS", "title": "AHU-7 差压再次上升", "detail": "ΔP 435 Pa", "severity": "warning", "evidence_id": "E-BMS-05", "sort_order": 12},
        {"id": "T13", "occurred_at": "2025-08-17T04:30:00+08:00", "source": "LIMS", "title": "PCR 菌种鉴定", "detail": "Micrococcus luteus — 人体来源", "severity": "danger", "evidence_id": "E-LIMS-01", "sort_order": 13},
        {"id": "T14", "occurred_at": "2025-08-17T06:00:00+08:00", "source": "—", "title": "偏差正式立案", "detail": "DEV-2025-08-147 创建", "severity": "danger", "sort_order": 14},
    ],
    "five_whys": [
        {"id": "FW1", "n": 1, "question": "为什么 Room-3B-Act-12 检出 M. luteus 超标？", "answer": "人体来源菌通过更衣环节进入 B 级区", "status": "accepted", "evidence_ids": ["E-LIMS-01", "E-EMS-05"], "is_root_cause": False, "ai_model": "claude-sonnet-4-20250514", "ai_output_hash": sha256_hex("reasoner-v1-fw1")},
        {"id": "FW2", "n": 2, "question": "为什么人体来源菌在晚班聚集？", "answer": "清洁人员 EMP-2187 更衣合规性下降 (CCTV 佐证 4 次)", "status": "accepted", "evidence_ids": ["E-CCTV-01","E-CCTV-02","E-CCTV-03","E-CCTV-04"], "is_root_cause": False, "ai_model": "claude-sonnet-4-20250514", "ai_output_hash": sha256_hex("reasoner-v1-fw2")},
        {"id": "FW3", "n": 3, "question": "为什么 EMP-2187 更衣不合规？", "answer": "更衣资质已逾期 (2025-08-10)，缺乏有效再培训", "status": "accepted", "evidence_ids": ["E-LMS-01"], "is_root_cause": False, "ai_model": "claude-sonnet-4-20250514", "ai_output_hash": sha256_hex("reasoner-v1-fw3")},
        {"id": "FW4", "n": 4, "question": "为什么培训逾期未触发系统告警？", "answer": "LMS 与排班系统无联动，逾期资质员工仍被排入 B 级区", "status": "pending", "evidence_ids": ["E-LMS-03","E-SYS-01"], "is_root_cause": False, "ai_model": "claude-sonnet-4-20250514", "ai_output_hash": sha256_hex("reasoner-v1-fw4")},
        {"id": "FW5", "n": 5, "question": "为什么 LMS 与排班系统缺乏资质门控联动？(根因)", "answer": "IT 系统架构设计时未将资质门控作为硬性集成项，仅依赖人工核查", "status": "pending", "evidence_ids": ["E-SYS-01"], "is_root_cause": True, "ai_model": "claude-sonnet-4-20250514", "ai_output_hash": sha256_hex("reasoner-v1-fw5")},
    ],
    "skeptic_challenges": [
        {"id": "SK1", "target": "Why 2: 清洁人员 EMP-2187 更衣合规性下降是菌落聚集主因", "challenge": "8/8 (资质逾期前 2 天) 也有同类菌落检出，但当天 EMP-2187 更衣正常。若更衣是唯一原因，为何资质有效期内也出现？HVAC 差压上升是否为并行因素？", "evidence_ids": ["E-BMS-02","E-BMS-05","E-CMMS-03"], "status": "pending"},
    ],
}

# ============================================================
#  CASE B — HPLC 杂质超标
# ============================================================
CASE_B = {
    "id": "DEV-2025-08-152",
    "title": "原料药 HPLC 杂质 A 超标 0.32%",
    "description": "原料药 AB-2025-301 HPLC 检测杂质 A 含量 0.32%，超过规格 0.25%。",
    "severity": "major",
    "status": "reviewing",
    "case_type": "LIMS + 工艺参数",
    "complexity": 2,
    "location": "API 车间 · 反应釜 R-204",
    "area": "原料药生产区",
    "shift": "白班",
    "investigator_id": "U-SM",
    "qa_reviewer_id": "U-DL",
    "discovered_at": "2025-08-16T11:20:00+08:00",
    "deadline": "2025-08-30T23:59:59+08:00",
    "product_impact": "AB-2025-301 (10kg) 待评估",
    "progress": 85,
    "hitl_gates": {"gate1": "done", "gate2": "done", "gate3": "done", "gate4": "pending", "gate5": "pending"},
    "meta": {"affectedBatches": 1},
    "evidence": [
        {"id": "E-LIMS-10", "source_system": "LIMS", "collected_at": "2025-08-16T10:30:00+08:00", "confidence": 4, "summary": "HPLC 杂质 A 0.32% (规格 ≤0.25%)", "raw": {"sample": "S-2025-08-16-105", "method": "HPLC-UV", "impurity": "A", "result": 0.32, "spec": 0.25, "status": "OOS"}},
        {"id": "E-MES-10", "source_system": "MES", "collected_at": "2025-08-15T22:00:00+08:00", "confidence": 4, "summary": "反应釜 R-204 温度波动 ±3°C (规格 ±1°C)", "raw": {"batch": "AB-2025-301", "step": "反应步骤 3", "param": "温度", "spec": "25±1°C", "actual": "23.8–27.2°C", "excursions": 4}},
        {"id": "E-CMMS-10", "source_system": "CMMS", "collected_at": "2025-08-15T18:00:00+08:00", "confidence": 3, "summary": "R-204 冷却夹垢上次清洗: 2025-04-10", "raw": {"work_order": "WO-20250410-0180", "type": "CIP", "asset": "R-204", "next_due": "2025-07-10 (逾期)"}},
    ],
    "timeline": [
        {"id": "TB1", "occurred_at": "2025-08-15T18:00:00+08:00", "source": "CMMS", "title": "R-204 CIP 逾期", "detail": "上次清洗 2025-04-10，已逾期 3 个月", "severity": "warning", "evidence_id": "E-CMMS-10", "sort_order": 1},
        {"id": "TB2", "occurred_at": "2025-08-15T22:00:00+08:00", "source": "MES", "title": "温度波动告警", "detail": "反应步骤 3 温度超出 ±1°C", "severity": "warning", "evidence_id": "E-MES-10", "sort_order": 2},
        {"id": "TB3", "occurred_at": "2025-08-16T10:30:00+08:00", "source": "LIMS", "title": "HPLC 杂质超标", "detail": "杂质 A 0.32% (规格 0.25%)", "severity": "danger", "evidence_id": "E-LIMS-10", "is_anomaly": True, "sort_order": 3},
        {"id": "TB4", "occurred_at": "2025-08-16T11:20:00+08:00", "source": "—", "title": "偏差立案", "detail": "DEV-2025-08-152 创建", "severity": "danger", "sort_order": 4},
    ],
    "five_whys": [
        {"id": "FWB1", "n": 1, "question": "为什么 HPLC 杂质 A 超标？", "answer": "反应步骤 3 温度波动导致副反应增加", "status": "accepted", "evidence_ids": ["E-LIMS-10","E-MES-10"], "is_root_cause": False, "ai_model": "claude-sonnet-4-20250514", "ai_output_hash": sha256_hex("reasoner-b1")},
        {"id": "FWB2", "n": 2, "question": "为什么反应温度波动？", "answer": "冷却夹套结垢导致热交换效率下降", "status": "pending", "evidence_ids": ["E-CMMS-10"], "is_root_cause": False, "ai_model": "claude-sonnet-4-20250514", "ai_output_hash": sha256_hex("reasoner-b2")},
        {"id": "FWB3", "n": 3, "question": "为什么冷却夹套结垢未及时清洗？(根因)", "answer": "CIP 周期管理依赖人工排程，逾期未触发系统告警", "status": "pending", "evidence_ids": ["E-CMMS-10"], "is_root_cause": True, "ai_model": "claude-sonnet-4-20250514", "ai_output_hash": sha256_hex("reasoner-b3")},
    ],
    "skeptic_challenges": [],
}

# ============================================================
#  CASE C — 尘埃粒子告警
# ============================================================
CASE_C = {
    "id": "DEV-2025-08-149",
    "title": "无菌灌装区尘埃粒子连续告警",
    "description": "ISO 5 灌装区 Fill-ISO5-01 连续 3 天 0.5μm 尘埃粒子告警。",
    "severity": "minor",
    "status": "investigating",
    "case_type": "EMS + HVAC + 维护",
    "complexity": 2,
    "location": "Fill-ISO5-01",
    "area": "无菌灌装区",
    "shift": "白班",
    "investigator_id": "U-LC",
    "qa_reviewer_id": "U-JW",
    "discovered_at": "2025-08-16T03:15:00+08:00",
    "deadline": "2025-08-29T23:59:59+08:00",
    "progress": 45,
    "hitl_gates": {"gate1": "done", "gate2": "pending", "gate3": "pending", "gate4": "pending", "gate5": "pending"},
    "meta": {},
    "evidence": [
        {"id": "E-EMS-10", "source_system": "EMS", "collected_at": "2025-08-14T10:00:00+08:00", "confidence": 4, "summary": "Fill-ISO5-01 0.5μm 粒子 12/m³ (警戒 10)", "raw": {"point": "Fill-ISO5-01", "size": "0.5μm", "value": 12, "alert": 10, "action": 35}},
        {"id": "E-CMMS-10C", "source_system": "CMMS", "collected_at": "2025-08-13T09:00:00+08:00", "confidence": 3, "summary": "HEPA 滤芯 H-14 上次更换: 2024-12-01 (使用 8 个月)", "raw": {"asset": "HEPA-H14-Fill-01", "installed": "2024-12-01", "lifetime_months": 12, "current_age_months": 8}},
    ],
    "timeline": [
        {"id": "TC1", "occurred_at": "2025-08-13T09:00:00+08:00", "source": "CMMS", "title": "HEPA H-14 维护记录查询", "detail": "已使用 8 个月", "severity": "info", "evidence_id": "E-CMMS-10C", "sort_order": 1},
        {"id": "TC2", "occurred_at": "2025-08-14T10:00:00+08:00", "source": "EMS", "title": "尘埃粒子首次告警", "detail": "12/m³ (警戒 10)", "severity": "warning", "evidence_id": "E-EMS-10", "sort_order": 2},
        {"id": "TC3", "occurred_at": "2025-08-15T11:00:00+08:00", "source": "EMS", "title": "尘埃粒子持续告警", "detail": "14/m³", "severity": "warning", "sort_order": 3},
        {"id": "TC4", "occurred_at": "2025-08-16T03:15:00+08:00", "source": "—", "title": "偏差立案", "detail": "DEV-2025-08-149 创建", "severity": "danger", "sort_order": 4},
    ],
    "five_whys": [
        {"id": "FWC1", "n": 1, "question": "为什么尘埃粒子连续告警？", "answer": "HEPA 滤芯效率下降，过滤能力不足", "status": "pending", "evidence_ids": ["E-EMS-10"], "is_root_cause": False, "ai_model": "claude-sonnet-4-20250514", "ai_output_hash": sha256_hex("reasoner-c1")},
        {"id": "FWC2", "n": 2, "question": "为什么 HEPA 效率下降？(根因)", "answer": "滤芯使用接近寿命末期，未提前更换", "status": "pending", "evidence_ids": ["E-CMMS-10C"], "is_root_cause": True, "ai_model": "claude-sonnet-4-20250514", "ai_output_hash": sha256_hex("reasoner-c2")},
    ],
    "skeptic_challenges": [],
}

# ============================================================
#  CASE D — 签名缺失（已关闭）
# ============================================================
CASE_D = {
    "id": "DEV-2025-08-150",
    "title": "3 批 MES 批记录签名缺失",
    "description": "AB-2025-305~307 三批包装记录缺少操作员签名。",
    "severity": "minor",
    "status": "closed",
    "case_type": "MES + 培训",
    "complexity": 1,
    "location": "包装线 P-102",
    "area": "包装区",
    "shift": "白班",
    "investigator_id": "U-SM",
    "qa_reviewer_id": "U-DL",
    "discovered_at": "2025-08-15T09:00:00+08:00",
    "deadline": "2025-08-22T23:59:59+08:00",
    "progress": 100,
    "hitl_gates": {"gate1": "done", "gate2": "done", "gate3": "done", "gate4": "done", "gate5": "done"},
    "meta": {},
    "evidence": [
        {"id": "E-MES-20", "source_system": "MES", "collected_at": "2025-08-15T09:30:00+08:00", "confidence": 4, "summary": "3 批记录缺签名: AB-305 step 7, AB-306 step 5, AB-307 step 8", "raw": {"batches": ["AB-2025-305","AB-2025-306","AB-2025-307"], "missing_steps": [7,5,8]}},
    ],
    "timeline": [
        {"id": "TD1", "occurred_at": "2025-08-15T09:00:00+08:00", "source": "MES", "title": "签名缺失检出", "detail": "3 批记录缺签名", "severity": "warning", "evidence_id": "E-MES-20", "is_anomaly": True, "sort_order": 1},
        {"id": "TD2", "occurred_at": "2025-08-15T14:00:00+08:00", "source": "—", "title": "操作员补签", "detail": "3 批已补签 + 再培训", "severity": "info", "sort_order": 2},
        {"id": "TD3", "occurred_at": "2025-08-17T16:00:00+08:00", "source": "—", "title": "案例关闭", "detail": "CAPA 完成: MES 强制签名门控", "severity": "info", "sort_order": 3},
    ],
    "five_whys": [
        {"id": "FWD1", "n": 1, "question": "为什么 3 批记录签名缺失？(根因)", "answer": "MES 允许跳过签名保存，无强制门控", "status": "accepted", "evidence_ids": ["E-MES-20"], "is_root_cause": True, "ai_model": "claude-sonnet-4-20250514", "ai_output_hash": sha256_hex("reasoner-d1")},
    ],
    "skeptic_challenges": [],
}

ALL_CASES = [CASE_A, CASE_B, CASE_C, CASE_D]


# ============================================================
#  种子加载器
# ============================================================
async def seed_database():
    """初始化数据库并加载种子数据"""
    print(f"[seed] 连接 PostgreSQL: {settings.postgres_host}:{settings.postgres_port}")
    conn = await asyncpg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        user=settings.postgres_user,
        password=settings.postgres_password,
        database=settings.postgres_db,
    )

    try:
        # 1. 加载 schema
        schema_path = Path(__file__).parent / "db" / "01_schema.sql"
        schema_sql = schema_path.read_text(encoding="utf-8")
        print(f"[seed] 执行 schema: {schema_path.name}")
        await conn.execute(schema_sql)

        # 2. 清空旧数据（保留表结构）
        print("[seed] 清空旧数据...")
        for table in ["signatures","agent_runs","skeptic_challenges","five_whys","timeline_events","evidence","audit_log","deviations","source_systems","users"]:
            await conn.execute(f'TRUNCATE TABLE {table} RESTART IDENTITY CASCADE')

        # 3. 加载用户（含密码 hash）
        from shared.security import hash_password
        print(f"[seed] 加载 {len(USERS)} 个用户...")
        for u in USERS:
            await conn.execute(
                """INSERT INTO users (id, email, name, role, dept, title, avatar, color, shift, cert, password_hash, is_active)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,TRUE)""",
                u["id"], u["email"], u["name"], u["role"], u["dept"], u["title"],
                u["avatar"], u["color"], u["shift"], u["cert"],
                hash_password(u["password"]),
            )

        # 4. 加载源系统
        print(f"[seed] 加载 {len(SOURCE_SYSTEMS)} 个源系统...")
        for s in SOURCE_SYSTEMS:
            await conn.execute(
                """INSERT INTO source_systems (code, label, icon, color, description, health_status, is_readonly)
                   VALUES ($1,$2,$3,$4,$5,$6,TRUE)""",
                s["code"], s["label"], s["icon"], s["color"], s["description"], s["health_status"],
            )

        # 5. 加载案例 + 证据 + 时间线 + 5Whys + Skeptic
        for case in ALL_CASES:
            print(f"[seed] 加载案例 {case['id']} ({case['title'][:30]}...)")
            await conn.execute(
                """INSERT INTO deviations
                   (id, title, description, severity, status, case_type, complexity, location, area, shift,
                    investigator_id, qa_reviewer_id, discovered_at, deadline, organism, organism_source,
                    product_impact, immediate_action, progress, hitl_gates, meta)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21)""",
                case["id"], case["title"], case["description"], case["severity"], case["status"],
                case["case_type"], case["complexity"], case["location"], case["area"], case["shift"],
                case["investigator_id"], case["qa_reviewer_id"], case["discovered_at"], case["deadline"],
                case.get("organism"), case.get("organism_source"), case.get("product_impact"),
                case.get("immediate_action"), case["progress"], json.dumps(case["hitl_gates"]),
                json.dumps(case["meta"]),
            )

            # 证据
            for ev in case["evidence"]:
                h = evidence_hash(ev["raw"])
                await conn.execute(
                    """INSERT INTO evidence (id, case_id, source_system, collected_at, summary, raw_data, content_hash, confidence)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
                    ev["id"], case["id"], ev["source_system"], ev["collected_at"],
                    ev["summary"], json.dumps(ev["raw"]), h, ev["confidence"],
                )

            # 时间线
            for t in case["timeline"]:
                await conn.execute(
                    """INSERT INTO timeline_events (id, case_id, occurred_at, source, title, detail, severity, evidence_id, is_anomaly, sort_order)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)""",
                    t["id"], case["id"], t["occurred_at"], t["source"], t["title"], t["detail"],
                    t["severity"], t.get("evidence_id"), t.get("is_anomaly", False), t["sort_order"],
                )

            # 5 Whys
            for w in case["five_whys"]:
                await conn.execute(
                    """INSERT INTO five_whys (id, case_id, n, question, answer, status, evidence_ids, is_root_cause, ai_model, ai_output_hash)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)""",
                    w["id"], case["id"], w["n"], w["question"], w["answer"], w["status"],
                    w["evidence_ids"], w["is_root_cause"], w.get("ai_model"), w.get("ai_output_hash"),
                )

            # Skeptic 挑战
            for sk in case["skeptic_challenges"]:
                await conn.execute(
                    """INSERT INTO skeptic_challenges (id, case_id, target, challenge, evidence_ids, status)
                       VALUES ($1,$2,$3,$4,$5,$6)""",
                    sk["id"], case["id"], sk["target"], sk["challenge"], sk["evidence_ids"], sk["status"],
                )

        # 6. 初始审计日志（hash 链起点）
        print("[seed] 写入初始审计日志...")
        initial_logs = [
            {"operator": "system", "action": "SYSTEM_INIT", "target": "schema", "detail": "数据库初始化 + 种子数据加载", "evidence_refs": []},
            {"operator": "system", "action": "CREATE_DEVIATION", "target": "DEV-2025-08-147", "detail": "EMS 自动告警触发偏差创建", "evidence_refs": ["E-EMS-05"]},
        ]
        prev_hash = "0" * 40
        for i, log in enumerate(initial_logs, 1):
            now_iso = datetime.now(timezone.utc).isoformat()
            parts = [prev_hash, str(i), log["operator"], log["action"], log["target"], log["detail"], now_iso, json.dumps(log["evidence_refs"])]
            current_hash = "sha256:" + hashlib.sha256("|".join(parts).encode()).hexdigest()
            log_id = f"LOG-INIT-{i:04d}"
            await conn.execute(
                """INSERT INTO audit_log (log_id, seq, operator, action, target, detail, evidence_refs, prev_hash, current_hash, created_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,now())""",
                log_id, i, log["operator"], log["action"], log["target"], log["detail"],
                log["evidence_refs"], prev_hash, current_hash,
            )
            prev_hash = current_hash

        # 7. 验证
        counts = {}
        for t in ["users","source_systems","deviations","evidence","timeline_events","five_whys","skeptic_challenges","audit_log"]:
            r = await conn.fetchval(f"SELECT count(*) FROM {t}")
            counts[t] = r
        print(f"[seed] 完成 ✓ 表行数: {counts}")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(seed_database())
