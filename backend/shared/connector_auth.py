"""Connector 服务鉴权 — Mock 源系统端点也必须受控访问

Connector 暴露员工培训/资质等 PII 数据，历史上完全无鉴权。
现统一要求 X-Service-Key（服务间调用密钥）：
- 配置了 service_api_key（prod 强制）→ 校验不通过即 403
- 未配置（本地开发）→ 放行并保留告警日志，开发体验不受影响
"""
from __future__ import annotations

from fastapi import HTTPException, Request


def require_connector_auth(request: Request) -> None:
    """FastAPI 依赖 —— 校验 X-Service-Key（作为 router 级 dependencies 注入）"""
    from .config import settings

    if not settings.service_api_key:
        # 本地开发模式：未配置服务密钥时放行（prod 下 config 层强制要求配置）
        return

    provided = request.headers.get("X-Service-Key", "")
    if not provided:
        raise HTTPException(status_code=403, detail="缺少 X-Service-Key 服务鉴权头")
    from .security import safe_compare

    if not safe_compare(provided, settings.service_api_key):
        raise HTTPException(status_code=403, detail="X-Service-Key 校验失败")
