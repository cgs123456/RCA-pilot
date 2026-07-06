"""EMS Connector — 环境监测系统 Mock 微服务

职责：
- 模拟 EMS（Environmental Monitoring System）API
- 提供沉降菌/浮游菌监测点、历史采样数据、超限事件查询
- 仅提供只读接口（read-only）
- Agent 通过此 Connector 拉取环境监测证据

Phase 1 实现：内存数据 + 静态响应
Phase 2 升级：对接真实 EMS REST API
"""
from __future__ import annotations
