"""结构化日志 — JSON 格式，便于 ELK 采集"""
from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from .config import settings


class JSONFormatter(logging.Formatter):
    """JSON 日志格式器 — GxP 要求日志可机器解析"""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "service": settings.service_name,
            "env": settings.env,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)
        # traceId 贯穿：与错误响应/审计日志可互相关联
        try:
            from .error_handler import get_trace_id

            trace = get_trace_id()
            if trace:
                log_entry["trace_id"] = trace
        except Exception:
            pass
        # 自定义字段
        for key, value in getattr(record, "extra_fields", {}).items():
            log_entry[key] = value
        return json.dumps(log_entry, ensure_ascii=False, default=str)


def setup_logging(service_name: str | None = None) -> logging.Logger:
    """初始化全局日志"""
    if service_name:
        settings.service_name = service_name

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))

    # 降低第三方库日志级别
    for noisy in ("uvicorn.access", "httpx", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logging.getLogger(settings.service_name)


def get_logger(name: str = __name__) -> logging.Logger:
    return logging.getLogger(name)
