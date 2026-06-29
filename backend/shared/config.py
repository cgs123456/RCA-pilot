"""统一配置管理 — 通过环境变量注入"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import List

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置（12-factor app：环境变量驱动）"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- 服务标识 ----
    service_name: str = Field(default="rca-pilot", description="当前服务名")
    env: str = Field(default="dev", description="运行环境: dev|staging|prod")
    debug: bool = Field(default=False)
    log_level: str = Field(default="INFO")

    # ---- 数据库 ----
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_user: str = "rca"
    postgres_password: str = "rca-secret"
    postgres_db: str = "rca_pilot"

    # ---- Redis ----
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_password: str = ""
    redis_db: int = 0

    # ---- JWT / 认证 ----
    jwt_secret: str = Field(
        default="dev-secret-change-in-prod",
        description="JWT 签名密钥，生产必须从 K8s Secret 注入",
    )
    jwt_algorithm: str = "HS256"
    jwt_access_ttl_minutes: int = 15
    jwt_refresh_ttl_days: int = 7
    mfa_required: bool = True

    # ---- CORS ----
    cors_origins: str = "http://localhost:5173,http://localhost:3000,http://localhost:8080"

    # ---- 服务端口 ----
    auth_service_port: int = 8001
    case_service_port: int = 8002
    evidence_service_port: int = 8003
    agent_service_port: int = 8004
    audit_service_port: int = 8005
    lms_connector_port: int = 8011

    # ---- 服务发现（Docker Compose 内服务名）----
    auth_service_url: str = "http://auth-service:8001"
    case_service_url: str = "http://case-service:8002"
    evidence_service_url: str = "http://evidence-service:8003"
    agent_service_url: str = "http://agent-service:8004"
    audit_service_url: str = "http://audit-service:8005"
    lms_connector_url: str = "http://lms-connector:8011"

    # ---- GxP 合规 ----
    audit_log_immutable: bool = True
    session_timeout_minutes: int = 15
    require_dual_signature: bool = True
    retention_years: int = 10

    # ---- LLM (Phase 2 启用) ----
    llm_provider: str = ""  # anthropic|openai|azure
    llm_api_key: str = ""
    llm_model: str = "claude-sonnet-4-20250514"

    # ---- 可观测性 ----
    sentry_dsn: str = Field(default="", description="Sentry DSN，空则不初始化")
    prometheus_enabled: bool = True
    unleash_url: str = Field(default="", description="Unleash Feature Flag 服务地址，空则禁用灰度")

    @computed_field
    @property
    def postgres_dsn(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @computed_field
    @property
    def redis_url(self) -> str:
        auth = f":{self.redis_password}@" if self.redis_password else ""
        return f"redis://{auth}{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @computed_field
    @property
    def cors_origins_list(self) -> List[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    """单例配置"""
    return Settings()


settings = get_settings()
