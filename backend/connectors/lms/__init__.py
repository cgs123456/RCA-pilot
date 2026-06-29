"""LMS Connector — Mock LMS 微服务

职责：
- 模拟 LMS（培训管理系统）API
- 提供员工资质查询、培训记录、更衣认证状态
- 仅提供只读接口（read-only）
- Agent 通过此 Connector 拉取证据

Phase 1 实现：内存数据 + 静态响应
Phase 2 升级：对接真实 LMS REST API
"""
