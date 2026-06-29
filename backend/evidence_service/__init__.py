"""Evidence Service — 证据统一查询微服务

职责：
- 证据 CRUD（写入由 Connector 触发）
- 跨源系统聚合查询
- 案例证据列表
- 时间线事件查询
- 调用 Connector 拉取增量数据（Phase 1 仅 LMS）
"""
