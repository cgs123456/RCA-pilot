"""MES Connector — 生产执行系统 Mock 微服务

职责：
- 模拟 MES（Manufacturing Execution System）API
- 提供批次信息、清洁记录、CCTV 更衣异常事件查询
- 仅提供只读接口（read-only）
- Agent 通过此 Connector 拉取生产/清洁/CCTV 证据

Phase 1 实现：内存数据 + 静态响应
Phase 2 升级：对接真实 MES REST API
"""
from __future__ import annotations
