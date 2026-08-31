"""Redis 惰性客户端 — 会话黑名单 / 分布式限流共享存储

设计：
- redis 连不上时返回 None，调用方必须优雅降级（安全特性退化为无 Redis 的旧行为，
  而不是整个服务不可用）
- 单进程缓存实例，连接失败短时间不再重试（避免每次请求都撞超时）
"""
from __future__ import annotations

import contextlib
import logging
import time

from .config import settings

logger = logging.getLogger(__name__)

_client: object | None = None
_last_failure_ts: float = 0.0
_RETRY_INTERVAL_SECONDS = 30.0


async def get_redis() -> object | None:
    """获取可用的 Redis 客户端；不可用时返回 None（调用方自行降级）"""
    global _client, _last_failure_ts

    if not settings.redis_enabled:
        return None

    if _client is not None:
        return _client

    # 最近刚失败过，冷却期内直接放弃（避免每个请求都等一次连接超时）
    if time.monotonic() - _last_failure_ts < _RETRY_INTERVAL_SECONDS:
        return None

    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=1.0,
            socket_timeout=1.0,
            health_check_interval=30,
        )
        await client.ping()
        _client = client
        logger.info("Redis 连接成功（token 黑名单/限流共享存储已启用）")
        return _client
    except ImportError:
        logger.warning("redis 包未安装，jti 黑名单与分布式限流降级为进程内存模式")
        _last_failure_ts = time.monotonic()
        return None
    except Exception as e:  # 连接失败属预期场景（本地开发常无 Redis）
        logger.warning("Redis 不可用（%s: %s），相关功能降级", type(e).__name__, e)
        _last_failure_ts = time.monotonic()
        return None


async def close_redis() -> None:
    """进程退出时关闭连接"""
    global _client
    if _client is not None:
        with contextlib.suppress(Exception):
            await _client.aclose()  # type: ignore[attr-defined]
        _client = None
