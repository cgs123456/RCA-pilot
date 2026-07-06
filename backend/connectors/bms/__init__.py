"""BMS Connector — 楼宇管理系统 HVAC 监控 Mock 微服务

职责：
- 模拟 BMS（Building Management System）API
- 提供 AHU（空气处理机组）温湿度、压差历史查询
- 仅提供只读接口（read-only）
- Agent 通过此 Connector 拉取 HVAC 证据

Phase 1 实现：内存数据 + 静态响应
Phase 2 升级：对接真实 BMS BACnet/Modbus 网关
"""
from __future__ import annotations
