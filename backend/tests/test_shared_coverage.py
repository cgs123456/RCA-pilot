"""shared 模块纯单元测试 — 补齐覆盖率缺口

覆盖模块与缺口：
- feature_flag：is_enabled 灰度百分比 / flag_context / 环境变量与文件覆盖 /
  reload_flags / Unleash 远程分支（mock）
- logging：JSONFormatter 各字段分支 + setup_logging
- metrics：MetricsMiddleware 计数/跳过路径/异常路径 + /metrics 端点连接池统计
- redis_client：禁用降级 / 缓存 / 冷却 / 连接失败 / ImportError / close_redis
- connector_auth：X-Service-Key 各分支
- database：get_db/db_session 的 commit 与回滚分支 + pool_snapshot
- service_base：create_app 参数组合 / 健康检查 / 就绪检查 / 默认 lifespan
- auth_middleware：get_client_ip 可信代理策略 / get_auth_context 校验失败分支
- error_handler：各异常处理器分支（422 清洗 / 404 / 403 / 500 / Sentry）
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException, Request
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel
from sqlalchemy import QueuePool, create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shared import feature_flag, redis_client
from shared.auth_middleware import (
    AuthContext,
    get_auth_context,
    get_client_ip,
    get_db_from_auth,
    require,
)
from shared.config import settings
from shared.connector_auth import require_connector_auth
from shared.database import db_session, get_db, pool_snapshot
from shared.error_codes import ErrorCode
from shared.error_handler import (
    TraceIdMiddleware,
    _sanitize_validation_errors,
    get_trace_id,
    register_exception_handlers,
)
from shared.exceptions import AuthError, ForbiddenError, NotFoundError, RCAError
from shared.logging import JSONFormatter, get_logger, setup_logging
from shared.metrics import MetricsMiddleware, register_metrics_endpoint
from shared.security import create_access_token
from shared.service_base import create_app


# ============================================================
# 全局状态隔离 —— feature_flag lru_cache + logging 根配置
# ============================================================
@pytest.fixture(autouse=True)
def _clear_feature_flag_cache():
    """每个测试前后清空 _load_flags 的 lru_cache，避免环境变量污染其他测试"""
    feature_flag._load_flags.cache_clear()
    yield
    feature_flag._load_flags.cache_clear()


@pytest.fixture(autouse=True)
def _restore_logging_state():
    """setup_logging 会改写 root handler/级别与 settings.service_name —— 用后恢复"""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_service_name = settings.service_name
    noisy = ("uvicorn.access", "httpx", "asyncio")
    saved_noisy = {name: logging.getLogger(name).level for name in noisy}
    yield
    root.handlers = saved_handlers
    root.setLevel(saved_level)
    settings.service_name = saved_service_name
    for name, level in saved_noisy.items():
        logging.getLogger(name).setLevel(level)


def _http_scope(headers: dict[str, str] | None = None, client: tuple[str, int] | None = ("127.0.0.1", 5000)):
    """构造最小 ASGI scope（starlette Request 用）"""
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {"type": "http", "method": "GET", "path": "/", "headers": raw, "query_string": b""}
    if client is not None:
        scope["client"] = client
    return scope


def _request(headers: dict[str, str] | None = None, client: tuple[str, int] | None = ("127.0.0.1", 5000)) -> Request:
    return Request(_http_scope(headers, client))


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _drive_lifespan(app: FastAPI) -> list[str]:
    """手动驱动 ASGI lifespan 协议（httpx ASGITransport 不会触发 lifespan）"""
    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait({"type": "lifespan.startup"})
    queue.put_nowait({"type": "lifespan.shutdown"})
    events: list[str] = []

    async def receive():
        return await queue.get()

    async def send(message):
        events.append(message["type"])

    scope = {"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.0"}, "state": {}}
    await app(scope, receive, send)
    return events


class EchoBody(BaseModel):
    """422 校验测试用的请求体（模块级定义 —— FastAPI 需要可解析的注解）"""

    name: str


# ============================================================
# feature_flag
# ============================================================
class TestFeatureFlag:
    def test_undefined_flag_returns_default(self):
        assert feature_flag.is_enabled("no_such_flag") is False
        assert feature_flag.is_enabled("no_such_flag", default=True) is True

    def test_enabled_flag_full_rollout(self):
        assert feature_flag.is_enabled("dashboard_v2_predictive") is True
        assert feature_flag.is_enabled("dashboard_v2_predictive", user_id="U-LC") is True

    def test_disabled_flag(self):
        assert feature_flag.is_enabled("skeptic_v2_enhanced") is False
        assert feature_flag.is_enabled("skeptic_v2_enhanced", user_id="U-LC") is False

    def test_env_override_percentage_rollout(self, monkeypatch):
        monkeypatch.setenv(
            "RCA_FEATURE_FLAGS", '{"skeptic_v2_enhanced": {"enabled": true, "rollout_percentage": 50}}'
        )
        feature_flag.reload_flags()
        expected = feature_flag._hash_user_id("U-LC") < 50
        assert feature_flag.is_enabled("skeptic_v2_enhanced", user_id="U-LC") is expected
        # 无用户上下文时灰度无法判定 → False
        assert feature_flag.is_enabled("skeptic_v2_enhanced") is False

    def test_env_override_zero_rollout(self, monkeypatch):
        monkeypatch.setenv(
            "RCA_FEATURE_FLAGS", '{"skeptic_v2_enhanced": {"enabled": true, "rollout_percentage": 0}}'
        )
        feature_flag.reload_flags()
        assert feature_flag.is_enabled("skeptic_v2_enhanced", user_id="U-LC") is False

    def test_env_override_adds_new_flag(self, monkeypatch):
        monkeypatch.setenv("RCA_FEATURE_FLAGS", '{"brand_new_flag": {"enabled": true}}')
        feature_flag.reload_flags()
        assert feature_flag.is_enabled("brand_new_flag") is True

    def test_env_override_invalid_json_ignored(self, monkeypatch):
        monkeypatch.setenv("RCA_FEATURE_FLAGS", "{not-valid-json")
        feature_flag.reload_flags()
        assert feature_flag.is_enabled("skeptic_v2_enhanced") is False  # 回落默认

    def test_invalid_rollout_percentage_raises(self, monkeypatch):
        monkeypatch.setenv(
            "RCA_FEATURE_FLAGS", '{"skeptic_v2_enhanced": {"enabled": true, "rollout_percentage": 200}}'
        )
        feature_flag.reload_flags()
        with pytest.raises(ValueError, match="rollout_percentage"):
            feature_flag.is_enabled("skeptic_v2_enhanced", user_id="U-LC")

    def test_invalid_rollout_type_raises(self, monkeypatch):
        monkeypatch.setenv(
            "RCA_FEATURE_FLAGS", '{"skeptic_v2_enhanced": {"enabled": true, "rollout_percentage": "abc"}}'
        )
        feature_flag.reload_flags()
        with pytest.raises(ValueError, match="rollout_percentage"):
            feature_flag.is_enabled("skeptic_v2_enhanced", user_id="U-LC")

    def test_file_load_overrides_and_adds(self, monkeypatch, tmp_path):
        flag_file = tmp_path / "flags.json"
        flag_file.write_text(
            '{"skeptic_v2_enhanced": {"enabled": true, "rollout_percentage": 100},'
            ' "file_only_flag": {"enabled": true}}',
            encoding="utf-8",
        )
        monkeypatch.setenv("RCA_FEATURE_FLAGS_FILE", str(flag_file))
        feature_flag.reload_flags()
        assert feature_flag.is_enabled("skeptic_v2_enhanced") is True
        assert feature_flag.is_enabled("file_only_flag") is True

    def test_file_missing_ignored(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RCA_FEATURE_FLAGS_FILE", str(tmp_path / "missing.json"))
        feature_flag.reload_flags()
        assert feature_flag.is_enabled("skeptic_v2_enhanced") is False

    def test_file_invalid_json_ignored(self, monkeypatch, tmp_path):
        flag_file = tmp_path / "flags.json"
        flag_file.write_text("{{broken", encoding="utf-8")
        monkeypatch.setenv("RCA_FEATURE_FLAGS_FILE", str(flag_file))
        feature_flag.reload_flags()
        assert feature_flag.is_enabled("skeptic_v2_enhanced") is False

    def test_hash_user_id_stable_and_bounded(self):
        value = feature_flag._hash_user_id("U-LC")
        assert 0 <= value <= 99
        assert feature_flag._hash_user_id("U-LC") == value

    def test_flag_context_list_flags_reload(self):
        ctx = feature_flag.flag_context("skeptic_v2_enhanced")
        assert ctx["enabled"] is False
        assert "description" in ctx
        assert feature_flag.flag_context("no_such_flag") == {}

        all_flags = feature_flag.list_flags()
        assert "dashboard_v2_predictive" in all_flags

        feature_flag.reload_flags()
        assert feature_flag.list_flags() == all_flags


class TestUnleashIntegration:
    @staticmethod
    def _install_fake_client(
        monkeypatch, *, init_raises=False, is_enabled_raises=False, is_enabled_ret=True
    ):
        calls = {"init": 0, "enabled": []}

        class FakeUnleashClient:
            def __init__(self, url=None, app_name=None):
                self.url = url

            def initialize_client(self):
                calls["init"] += 1  # type: ignore[operator]
                if init_raises:
                    raise RuntimeError("unleash unreachable")

            def is_enabled(self, name, context=None):
                calls["enabled"].append((name, context))  # type: ignore[attr-defined]
                if is_enabled_raises:
                    raise RuntimeError("unleash check failed")
                return is_enabled_ret

        fake_mod = ModuleType("UnleashClient")
        fake_mod.UnleashClient = FakeUnleashClient  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "UnleashClient", fake_mod)
        monkeypatch.setattr(feature_flag, "_unleash_client", None)
        monkeypatch.setattr(feature_flag, "_unleash_failed", False)
        return calls

    def test_get_client_success_and_cached(self, monkeypatch):
        calls = self._install_fake_client(monkeypatch)
        client = feature_flag._get_unleash_client()
        assert client is not None
        assert calls["init"] == 1
        # 第二次直接复用单例，不再初始化
        assert feature_flag._get_unleash_client() is client
        assert calls["init"] == 1

    def test_get_client_failure_no_retry(self, monkeypatch):
        calls = self._install_fake_client(monkeypatch, init_raises=True)
        assert feature_flag._get_unleash_client() is None
        assert feature_flag._unleash_failed is True
        # 失败后不再反复重试
        assert feature_flag._get_unleash_client() is None
        assert calls["init"] == 1

    def test_is_enabled_uses_unleash_with_context(self, monkeypatch):
        calls = self._install_fake_client(monkeypatch)
        monkeypatch.setattr(settings, "unleash_url", "http://unleash.example.com")
        assert feature_flag.is_enabled("any_remote_flag", user_id="U-LC") is True
        assert ("any_remote_flag", {"userId": "U-LC"}) in calls["enabled"]

    def test_is_enabled_unleash_exception_falls_back(self, monkeypatch):
        self._install_fake_client(monkeypatch, is_enabled_raises=True)
        monkeypatch.setattr(settings, "unleash_url", "http://unleash.example.com")
        # Unleash 判定异常 → 降级本地配置（dashboard_v2_predictive 默认全量）
        assert feature_flag.is_enabled("dashboard_v2_predictive") is True

    def test_is_enabled_unleash_none_client_falls_back(self, monkeypatch):
        monkeypatch.setattr(feature_flag, "_get_unleash_client", lambda: None)
        monkeypatch.setattr(settings, "unleash_url", "http://unleash.example.com")
        assert feature_flag.is_enabled("dashboard_v2_predictive") is True


# ============================================================
# logging
# ============================================================
class TestJSONFormatter:
    @staticmethod
    def _make_record(msg="hello", exc_info=None) -> logging.LogRecord:
        return logging.LogRecord(
            "fmt.test", logging.INFO, "fake_path.py", 42, msg, (), exc_info
        )

    def test_basic_fields(self):
        entry = json.loads(JSONFormatter().format(self._make_record("服务启动")))
        for key in (
            "timestamp",
            "level",
            "service",
            "env",
            "logger",
            "message",
            "module",
            "function",
            "line",
        ):
            assert key in entry
        assert entry["level"] == "INFO"
        assert entry["message"] == "服务启动"
        assert entry["module"] == "fake_path"
        assert entry["line"] == 42

    def test_exception_field(self):
        try:
            raise ValueError("boom")
        except ValueError:
            entry = json.loads(JSONFormatter().format(self._make_record(exc_info=sys.exc_info())))
        assert "boom" in entry["exception"]

    def test_trace_id_field(self):
        from shared.error_handler import trace_id_ctx

        trace_id_ctx.set("trace-fmt-1")
        try:
            entry = json.loads(JSONFormatter().format(self._make_record()))
            assert entry["trace_id"] == "trace-fmt-1"
        finally:
            trace_id_ctx.set("")

    def test_trace_id_lookup_error_tolerated(self, monkeypatch):
        def _raise():
            raise RuntimeError("no trace context")

        monkeypatch.setattr("shared.error_handler.get_trace_id", _raise)
        entry = json.loads(JSONFormatter().format(self._make_record()))
        assert "trace_id" not in entry

    def test_extra_fields(self):
        record = self._make_record("带扩展字段")
        record.extra_fields = {"user_id": "U-LC", "case_id": "DEV-1"}
        entry = json.loads(JSONFormatter().format(record))
        assert entry["user_id"] == "U-LC"
        assert entry["case_id"] == "DEV-1"


class TestSetupLogging:
    def test_setup_with_service_name(self):
        logger = setup_logging("unit-log-svc")
        root = logging.getLogger()
        assert logger.name == "unit-log-svc"
        assert settings.service_name == "unit-log-svc"

        assert len(root.handlers) == 1
        handler = root.handlers[0]
        assert isinstance(handler, logging.StreamHandler)
        assert handler.stream is sys.stdout
        assert isinstance(handler.formatter, JSONFormatter)

        expected_level = getattr(logging, settings.log_level.upper(), logging.INFO)
        assert root.level == expected_level
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("asyncio").level == logging.WARNING

    def test_setup_without_name_keeps_service(self):
        before = settings.service_name
        logger = setup_logging()
        assert settings.service_name == before
        assert logger.name == before

    def test_get_logger(self):
        assert get_logger("unit-logger-x").name == "unit-logger-x"
        assert get_logger("unit-logger-x") is logging.getLogger("unit-logger-x")


# ============================================================
# metrics
# ============================================================
class TestMetricsMiddleware:
    @staticmethod
    def _build_app(service_name: str) -> FastAPI:
        app = FastAPI()
        app.add_middleware(MetricsMiddleware, service_name=service_name)
        register_metrics_endpoint(app, service_name)

        @app.get("/ping")
        async def ping():
            return {"ok": True}

        @app.get("/boom")
        async def boom():
            raise ValueError("boom")

        return app

    async def test_records_request_metrics(self):
        app = self._build_app("metrics-unit-a")
        async with await _client(app) as client:
            resp = await client.get("/ping")
            assert resp.status_code == 200
            metrics = await client.get("/metrics")

        assert metrics.status_code == 200
        content = metrics.content
        assert b"http_requests_total" in content
        assert b'endpoint="/ping"' in content
        assert b"http_request_duration_seconds" in content

    async def test_meta_paths_skipped(self):
        app = self._build_app("metrics-unit-b")
        async with await _client(app) as client:
            resp = await client.get("/metrics")
            assert resp.status_code == 200
            docs = await client.get("/docs")
            assert docs.status_code == 200
        # /metrics 与 /docs 本身不计入业务指标
        assert b'endpoint="/metrics"' not in resp.content

    async def test_unmatched_path_normalized(self):
        app = self._build_app("metrics-unit-c")
        async with await _client(app) as client:
            missing = await client.get("/no-such-path")
            assert missing.status_code == 404
            metrics = await client.get("/metrics")
        assert b'endpoint="unmatched"' in metrics.content

    async def test_exception_records_5xx(self):
        app = self._build_app("metrics-unit-d")
        async with await _client(app) as client:
            with pytest.raises(ValueError, match="boom"):
                await client.get("/boom")
            metrics = await client.get("/metrics")
        assert b"http_errors_total" in metrics.content
        assert b'status="500"' in metrics.content

    async def test_metrics_endpoint_pool_stats(self, monkeypatch):
        """连接池为 QueuePool 时更新 db_pool_* 指标"""
        sync_engine = create_engine("sqlite://", poolclass=QueuePool)
        monkeypatch.setattr("shared.database.engine", sync_engine)

        app = FastAPI()
        register_metrics_endpoint(app, "metrics-unit-e")
        async with await _client(app) as client:
            resp = await client.get("/metrics")
        assert resp.status_code == 200
        assert b"db_pool_size" in resp.content

    async def test_metrics_endpoint_pool_error_tolerated(self, monkeypatch):
        """连接池读取异常时 /metrics 仍可用"""
        monkeypatch.setattr("shared.database.engine", None)

        app = FastAPI()
        register_metrics_endpoint(app, "metrics-unit-f")
        async with await _client(app) as client:
            resp = await client.get("/metrics")
        assert resp.status_code == 200
        assert b"http_requests_total" in resp.content or b"db_pool" in resp.content

    async def test_metrics_service_name_falls_back_to_title(self):
        app = FastAPI()
        register_metrics_endpoint(app)  # service_name 为空 → 用 app.title
        async with await _client(app) as client:
            resp = await client.get("/metrics")
        assert resp.status_code == 200
        assert len(resp.content) > 0


# ============================================================
# redis_client
# ============================================================
class TestRedisClient:
    async def test_disabled_returns_none(self):
        assert settings.redis_enabled is False
        assert await redis_client.get_redis() is None

    async def test_cached_client_returned(self, monkeypatch):
        fake = SimpleNamespace(aclose=AsyncMock())
        monkeypatch.setattr(settings, "redis_enabled", True)
        monkeypatch.setattr(redis_client, "_client", fake)
        monkeypatch.setattr(redis_client, "_last_failure_ts", 0.0)
        assert await redis_client.get_redis() is fake

    async def test_cooldown_returns_none(self, monkeypatch):
        monkeypatch.setattr(settings, "redis_enabled", True)
        monkeypatch.setattr(redis_client, "_client", None)
        monkeypatch.setattr(redis_client, "_last_failure_ts", time.monotonic())
        assert await redis_client.get_redis() is None

    async def test_connect_success_caches_client(self, monkeypatch):
        import redis.asyncio as aioredis_module

        monkeypatch.setattr(settings, "redis_enabled", True)
        monkeypatch.setattr(redis_client, "_client", None)
        monkeypatch.setattr(redis_client, "_last_failure_ts", 0.0)

        def fake_from_url(*args, **kwargs):
            return SimpleNamespace(ping=AsyncMock(return_value=True))

        monkeypatch.setattr(aioredis_module, "from_url", fake_from_url)
        client = await redis_client.get_redis()
        assert client is not None
        # 第二次直接复用
        assert await redis_client.get_redis() is client

    async def test_connect_failure_degrades(self, monkeypatch):
        import redis.asyncio as aioredis_module

        monkeypatch.setattr(settings, "redis_enabled", True)
        monkeypatch.setattr(redis_client, "_client", None)
        monkeypatch.setattr(redis_client, "_last_failure_ts", 0.0)

        async def fake_ping():
            raise ConnectionError("connection refused")

        def fake_from_url(*args, **kwargs):
            return SimpleNamespace(ping=fake_ping)

        monkeypatch.setattr(aioredis_module, "from_url", fake_from_url)
        assert await redis_client.get_redis() is None
        assert redis_client._last_failure_ts > 0

    async def test_import_error_degrades(self, monkeypatch):
        monkeypatch.setattr(settings, "redis_enabled", True)
        monkeypatch.setattr(redis_client, "_client", None)
        monkeypatch.setattr(redis_client, "_last_failure_ts", 0.0)
        # sys.modules 置 None → import 直接 ImportError
        monkeypatch.setitem(sys.modules, "redis.asyncio", None)
        assert await redis_client.get_redis() is None

    async def test_close_redis_closes_client(self, monkeypatch):
        fake = SimpleNamespace(aclose=AsyncMock())
        monkeypatch.setattr(redis_client, "_client", fake)
        await redis_client.close_redis()
        fake.aclose.assert_awaited_once()
        assert redis_client._client is None

    async def test_close_redis_without_client(self, monkeypatch):
        monkeypatch.setattr(redis_client, "_client", None)
        await redis_client.close_redis()  # 无客户端时不抛异常

    async def test_close_redis_suppresses_errors(self, monkeypatch):
        fake = SimpleNamespace(aclose=AsyncMock(side_effect=RuntimeError("close failed")))
        monkeypatch.setattr(redis_client, "_client", fake)
        await redis_client.close_redis()  # 关闭异常被吞掉


# ============================================================
# connector_auth
# ============================================================
class TestConnectorAuth:
    def test_no_key_configured_allows(self, monkeypatch):
        monkeypatch.setattr(settings, "service_api_key", "")
        assert require_connector_auth(_request()) is None  # type: ignore[func-returns-value]

    def test_missing_header_rejected(self, monkeypatch):
        monkeypatch.setattr(settings, "service_api_key", "svc-secret-key-1")
        with pytest.raises(HTTPException) as exc:
            require_connector_auth(_request())
        assert exc.value.status_code == 403
        assert "缺少" in str(exc.value.detail)

    def test_wrong_key_rejected(self, monkeypatch):
        monkeypatch.setattr(settings, "service_api_key", "svc-secret-key-1")
        with pytest.raises(HTTPException) as exc:
            require_connector_auth(_request({"X-Service-Key": "wrong-key"}))
        assert exc.value.status_code == 403
        assert "校验失败" in str(exc.value.detail)

    def test_correct_key_allowed(self, monkeypatch):
        monkeypatch.setattr(settings, "service_api_key", "svc-secret-key-1")
        assert require_connector_auth(_request({"X-Service-Key": "svc-secret-key-1"})) is None  # type: ignore[func-returns-value]


# ============================================================
# database
# ============================================================
class TestDatabaseSessions:
    @staticmethod
    def _sqlite_factory(test_engine):
        return async_sessionmaker(
            test_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )

    async def test_get_db_commit_path(self, test_engine, monkeypatch):
        monkeypatch.setattr("shared.database.AsyncSessionLocal", self._sqlite_factory(test_engine))
        agen = get_db()
        session = await agen.__anext__()
        assert isinstance(session, AsyncSession)
        # 生成器正常结束 → commit + StopAsyncIteration
        with pytest.raises(StopAsyncIteration):
            await agen.__anext__()

    async def test_get_db_rollback_path(self, test_engine, monkeypatch):
        monkeypatch.setattr("shared.database.AsyncSessionLocal", self._sqlite_factory(test_engine))
        agen = get_db()
        await agen.__anext__()
        with pytest.raises(ValueError, match="boom"):
            await agen.athrow(ValueError("boom"))

    async def test_db_session_commit_path(self, test_engine, monkeypatch):
        monkeypatch.setattr("shared.database.AsyncSessionLocal", self._sqlite_factory(test_engine))
        async with db_session() as session:
            assert isinstance(session, AsyncSession)

    async def test_db_session_rollback_path(self, test_engine, monkeypatch):
        monkeypatch.setattr("shared.database.AsyncSessionLocal", self._sqlite_factory(test_engine))
        with pytest.raises(ValueError, match="boom"):
            async with db_session():
                raise ValueError("boom")

    def test_pool_snapshot_queue_pool(self, monkeypatch):
        sync_engine = create_engine("sqlite://", poolclass=QueuePool)
        monkeypatch.setattr("shared.database.engine", sync_engine)
        snap = pool_snapshot()
        assert set(snap) == {"pool_size", "checked_in", "checked_out", "overflow"}
        assert snap["pool_size"] == 5
        assert snap["checked_out"] == 0

    def test_pool_snapshot_non_queue_pool(self, monkeypatch):
        monkeypatch.setattr("shared.database.engine", SimpleNamespace(pool=object()))
        assert pool_snapshot() == {
            "pool_size": 0,
            "checked_in": 0,
            "checked_out": 0,
            "overflow": 0,
        }


# ============================================================
# service_base
# ============================================================
class TestCreateApp:
    async def test_health_endpoint(self):
        app = create_app("unit-svc-base")
        async with await _client(app) as client:
            resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "service": "unit-svc-base", "env": settings.env}

    async def test_metrics_and_rate_limit_disabled(self):
        app = create_app("unit-svc-nofx", enable_metrics=False, enable_rate_limit=False)
        async with await _client(app) as client:
            health = await client.get("/health")
            metrics = await client.get("/metrics")
        assert health.status_code == 200
        assert metrics.status_code == 404  # metrics 未注册

    async def test_version_param_used(self):
        app = create_app("unit-svc-ver", version="9.9.9")
        assert app.version == "9.9.9"
        assert "unit-svc-ver" in app.title

    async def test_prod_mode_closes_docs(self, monkeypatch):
        monkeypatch.setattr(settings, "env", "prod")
        app = create_app("unit-svc-prod")
        assert app.docs_url is None
        assert app.redoc_url is None
        assert app.openapi_url is None

    async def test_readiness_ok(self, test_engine, monkeypatch):
        factory = async_sessionmaker(
            test_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )
        monkeypatch.setattr("shared.database.AsyncSessionLocal", factory)
        app = create_app("unit-svc-ready")
        async with await _client(app) as client:
            resp = await client.get("/ready")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ready"
        assert body["db"] == "ok"

    async def test_readiness_db_fail_returns_503(self, monkeypatch):
        class _FailingSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def execute(self, *args, **kwargs):
                raise RuntimeError("db down")

        monkeypatch.setattr("shared.database.AsyncSessionLocal", lambda: _FailingSession())
        app = create_app("unit-svc-notready")
        async with await _client(app) as client:
            resp = await client.get("/ready")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "not_ready"
        assert "RuntimeError" in body["db"]

    async def test_default_lifespan_with_sentry(self, monkeypatch):
        fake_sdk = ModuleType("sentry_sdk")
        fake_sdk.init = MagicMock()  # type: ignore[attr-defined]
        fake_integrations = ModuleType("sentry_sdk.integrations")
        fake_fastapi_int = ModuleType("sentry_sdk.integrations.fastapi")
        fake_fastapi_int.FastApiIntegration = MagicMock()  # type: ignore[attr-defined]
        fake_sa_int = ModuleType("sentry_sdk.integrations.sqlalchemy")
        fake_sa_int.SqlalchemyIntegration = MagicMock()  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "sentry_sdk", fake_sdk)
        monkeypatch.setitem(sys.modules, "sentry_sdk.integrations", fake_integrations)
        monkeypatch.setitem(sys.modules, "sentry_sdk.integrations.fastapi", fake_fastapi_int)
        monkeypatch.setitem(sys.modules, "sentry_sdk.integrations.sqlalchemy", fake_sa_int)
        monkeypatch.setattr(settings, "sentry_dsn", "https://fake@example.com/1")

        app = create_app("unit-svc-lifespan")
        events = await _drive_lifespan(app)

        assert "lifespan.startup.complete" in events
        assert "lifespan.shutdown.complete" in events
        fake_sdk.init.assert_called_once()
        kwargs = fake_sdk.init.call_args.kwargs
        assert kwargs["dsn"] == "https://fake@example.com/1"
        assert kwargs["environment"] == settings.env
        assert kwargs["send_default_pii"] is False

    async def test_custom_lifespan_used(self):
        seen: list[str] = []

        @asynccontextmanager
        async def custom_lifespan(app: FastAPI):
            seen.append("startup")
            yield
            seen.append("shutdown")

        app = create_app("unit-svc-custom", lifespan=custom_lifespan)
        events = await _drive_lifespan(app)
        assert events == ["lifespan.startup.complete", "lifespan.shutdown.complete"]
        assert seen == ["startup", "shutdown"]


# ============================================================
# auth_middleware
# ============================================================
class TestGetClientIp:
    def test_untrusted_proxy_ignores_forwarded_headers(self, monkeypatch):
        """不可信代理头直接忽略 —— 防伪造 XFF 绕过限流"""
        monkeypatch.setattr(settings, "trust_proxy_headers", False)
        req = _request({"X-Forwarded-For": "6.6.6.6", "X-Real-IP": "7.7.7.7"}, client=("10.0.0.5", 40000))
        assert get_client_ip(req) == "10.0.0.5"

    def test_trusted_proxy_takes_rightmost_xff(self, monkeypatch):
        monkeypatch.setattr(settings, "trust_proxy_headers", True)
        req = _request({"X-Forwarded-For": "6.6.6.6, 8.8.8.8"}, client=("10.0.0.5", 40000))
        assert get_client_ip(req) == "8.8.8.8"

    def test_trusted_proxy_x_real_ip(self, monkeypatch):
        monkeypatch.setattr(settings, "trust_proxy_headers", True)
        req = _request({"X-Real-IP": "9.9.9.9"}, client=("10.0.0.5", 40000))
        assert get_client_ip(req) == "9.9.9.9"

    def test_trusted_proxy_without_headers_falls_back(self, monkeypatch):
        monkeypatch.setattr(settings, "trust_proxy_headers", True)
        assert get_client_ip(_request(client=("10.0.0.5", 40000))) == "10.0.0.5"

    def test_no_client_returns_unknown(self):
        assert get_client_ip(_request(client=None)) == "unknown"


class TestGetAuthContext:
    async def test_success_builds_context(self, test_db, seed_db):
        user = seed_db[0]
        token = create_access_token(user.id, claims={"email": user.email, "role": user.role})
        ctx = await get_auth_context(
            request=_request(), authorization=f"Bearer {token}", db=test_db
        )
        assert ctx.user_id == "U-LC"
        assert ctx.email == user.email
        assert ctx.role == "investigator"
        assert "case:read" in ctx.permissions
        assert ctx.db is test_db
        assert ctx.ip_address == "127.0.0.1"

    async def test_missing_authorization_header(self, test_db):
        with pytest.raises(AuthError, match="缺少 Authorization"):
            await get_auth_context(request=_request(), authorization=None, db=test_db)

    async def test_non_bearer_scheme_rejected(self, test_db):
        with pytest.raises(AuthError, match="缺少 Authorization"):
            await get_auth_context(request=_request(), authorization="Basic abc", db=test_db)

    async def test_revoked_jti_rejected(self, test_db, seed_db, monkeypatch):
        async def fake_revoked(jti):
            return True

        monkeypatch.setattr("shared.security.is_jti_revoked", fake_revoked)
        user = seed_db[0]
        token = create_access_token(user.id, claims={})
        with pytest.raises(AuthError, match="会话已注销"):
            await get_auth_context(
                request=_request(), authorization=f"Bearer {token}", db=test_db
            )

    async def test_missing_sub_rejected(self, test_db, monkeypatch):
        def fake_decode(token, expected_type="access"):
            return {"jti": "j-no-sub", "type": "access"}

        monkeypatch.setattr("shared.auth_middleware.decode_token", fake_decode)
        with pytest.raises(AuthError, match="缺少 sub"):
            await get_auth_context(
                request=_request(), authorization="Bearer anything", db=test_db
            )

    async def test_unknown_user_rejected(self, test_db):
        token = create_access_token("U-GHOST-9", claims={})
        with pytest.raises(AuthError, match="用户不存在或已禁用"):
            await get_auth_context(
                request=_request(), authorization=f"Bearer {token}", db=test_db
            )

    async def test_session_timeout_rejected(self, test_db, seed_db, monkeypatch):
        user = seed_db[0]
        old_iat = int((datetime.now(UTC) - timedelta(hours=3)).timestamp())

        def fake_decode(token, expected_type="access"):
            return {"jti": "j-old", "sub": user.id, "iat": old_iat, "type": "access"}

        monkeypatch.setattr("shared.auth_middleware.decode_token", fake_decode)
        with pytest.raises(AuthError, match="会话超时"):
            await get_auth_context(request=_request(), authorization="Bearer t", db=test_db)

    async def test_missing_iat_skips_timeout_check(self, test_db, seed_db, monkeypatch):
        user = seed_db[0]

        def fake_decode(token, expected_type="access"):
            return {"jti": "j-noiat", "sub": user.id, "type": "access"}

        monkeypatch.setattr("shared.auth_middleware.decode_token", fake_decode)
        ctx = await get_auth_context(request=_request(), authorization="Bearer t", db=test_db)
        assert ctx.user_id == user.id

    async def test_get_db_from_auth_returns_session(self, test_db, seed_db):
        ctx = AuthContext(
            user=seed_db[0],
            session_id="s",
            ip_address="127.0.0.1",
            permissions=set(),
            db=test_db,
        )
        assert await get_db_from_auth(ctx) is test_db

    async def test_require_dependency(self, test_db, seed_db):
        ctx = AuthContext(
            user=seed_db[0],
            session_id="s",
            ip_address="127.0.0.1",
            permissions=set(),
            db=test_db,
        )
        allowed = require("case:read")
        assert await allowed(ctx) is ctx

        denied = require("user:manage")  # investigator 无此权限
        with pytest.raises(ForbiddenError):
            await denied(ctx)


# ============================================================
# error_handler
# ============================================================
class TestErrorHandlers:
    @staticmethod
    def _app_with(exc: BaseException) -> FastAPI:
        app = FastAPI()
        register_exception_handlers(app)

        @app.get("/boom")
        async def boom():
            raise exc

        return app

    @staticmethod
    async def _hit(app: FastAPI, *, raise_app_exceptions: bool = True):
        """访问 /boom；ExceptionHandler(Exception) 由 ServerErrorMiddleware 兜底，
        发送 500 响应后会向上重抛 —— 测 500 响应时关闭 raise_app_exceptions。"""
        transport = ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/boom")

    async def test_validation_error_sanitized(self):
        app = FastAPI()
        app.add_middleware(TraceIdMiddleware)
        register_exception_handlers(app)

        @app.post("/echo")
        async def echo(body: EchoBody):
            return {"name": body.name}

        async with await _client(app) as client:
            resp = await client.post("/echo", json={"name": None})
        assert resp.status_code == 422
        body = resp.json()
        assert body["code"] == "E4220"
        assert "请求参数校验失败" in body["message"]
        # 不回显提交值；traceId 贯穿
        assert '"input"' not in resp.text
        assert resp.headers["X-Trace-Id"] == body["trace_id"]

    async def test_not_found_error_mapped(self):
        async with await _client(self._app_with(NotFoundError("案例不存在"))) as client:
            resp = await client.get("/boom")
        assert resp.status_code == 404
        assert resp.json()["code"] == "E4040"
        assert "案例不存在" in resp.json()["message"]

    async def test_structured_error_code_wins(self):
        exc = AuthError("账号已锁定", error_code=ErrorCode.ACCOUNT_LOCKED.value)
        async with await _client(self._app_with(exc)) as client:
            resp = await client.get("/boom")
        assert resp.status_code == 401
        assert resp.json()["code"] == "E4013"

    async def test_invalid_error_code_falls_back_to_internal(self):
        exc = RCAError("奇怪的失败", error_code="E9999")
        async with await _client(self._app_with(exc)) as client:
            resp = await client.get("/boom")
        assert resp.status_code == 500
        assert resp.json()["code"] == "E5000"

    async def test_permission_error_403(self):
        async with await _client(self._app_with(PermissionError("没有导出权限"))) as client:
            resp = await client.get("/boom")
        assert resp.status_code == 403
        assert resp.json()["code"] == "E4030"

    async def test_generic_exception_debug_detail(self, monkeypatch):
        fake_sdk = ModuleType("sentry_sdk")
        fake_sdk.capture_exception = MagicMock()  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "sentry_sdk", fake_sdk)
        monkeypatch.setattr(settings, "debug", True)

        resp = await self._hit(
            self._app_with(RuntimeError("内部数据库损坏")), raise_app_exceptions=False
        )
        assert resp.status_code == 500
        body = resp.json()
        assert body["code"] == "E5000"
        assert body["detail"] == "内部数据库损坏"
        fake_sdk.capture_exception.assert_called_once()

    async def test_generic_exception_prod_masks_detail(self, monkeypatch):
        monkeypatch.setattr(settings, "debug", False)
        # sys.modules 置 None → import sentry_sdk 抛 ImportError
        monkeypatch.setitem(sys.modules, "sentry_sdk", None)

        resp = await self._hit(
            self._app_with(RuntimeError("secret-stack")), raise_app_exceptions=False
        )
        assert resp.status_code == 500
        body = resp.json()
        assert body["code"] == "E5000"
        assert body["detail"] == "请联系管理员并提供 trace_id 排查"
        assert "secret-stack" not in resp.text

    def test_sanitize_removes_input_and_url(self):
        cleaned = _sanitize_validation_errors(
            [{"input": "secret", "url": "http://x", "msg": "bad", "loc": ["body", "name"]}]
        )
        assert cleaned == [{"msg": "bad", "loc": ["body", "name"]}]

    def test_sanitize_keeps_non_dict(self):
        assert _sanitize_validation_errors(["plain", 42]) == ["plain", 42]


class TestTraceIdMiddleware:
    @staticmethod
    def _app() -> FastAPI:
        app = FastAPI()
        app.add_middleware(TraceIdMiddleware)

        @app.get("/ping")
        async def ping():
            return {"trace_id": get_trace_id()}

        return app

    async def test_propagates_client_trace_id(self):
        async with await _client(self._app()) as client:
            resp = await client.get("/ping", headers={"X-Trace-Id": "client-trace-42"})
        assert resp.headers["X-Trace-Id"] == "client-trace-42"
        assert resp.json()["trace_id"] == "client-trace-42"

    async def test_generates_trace_id_when_absent(self):
        async with await _client(self._app()) as client:
            resp = await client.get("/ping")
        trace_id = resp.headers["X-Trace-Id"]
        assert len(trace_id) == 32
        int(trace_id, 16)  # 合法 hex（uuid4().hex）
        assert resp.json()["trace_id"] == trace_id

    def test_get_trace_id_outside_request_is_empty(self):
        assert get_trace_id() == ""
