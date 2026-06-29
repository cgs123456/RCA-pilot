"""结构化日志 — JSON 格式，便于 ELK 采集"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Dict

from .config import settings


class JSONFormatter(logging.Formatter):
    """JSON 日志格式器 — GxP 要求日志可机器解析"""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
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
