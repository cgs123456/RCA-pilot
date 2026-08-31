"""统一配置管理 — 通过环境变量注入"""
from __future__ import annotations

from functools import lru_cache

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
    redis_enabled: bool = Field(default=True, description="False 时 jti 黑名单/限流共享存储直接降级（测试/离线环境）")

    # ---- JWT / 认证 ----
    jwt_secret: str = Field(
        default="dev-secret-change-in-prod",
        description="JWT 签名密钥，生产必须从 K8s Secret 注入",
    )
    jwt_algorithm: str = "HS256"
    jwt_access_ttl_minutes: int = 15
    jwt_refresh_ttl_days: int = 7
    # MFA：代码尚未实现 TOTP 校验（auth_service 登录流程不消费 mfa_code），
    # 默认必须为 False，否则构成"声明与实现不符"的 GxP 审计发现。
    # 实现 TOTP 后再改回 True。
    mfa_required: bool = False
    # 会话锁定：连续失败锁定时长（分钟），0 表示永久锁定（不推荐）
    account_lockout_minutes: int = 15
    # bcrypt 哈希成本因子（2^cost 轮）。生产建议 12，测试/CI 可调低加速
    bcrypt_cost: int = Field(default=12, description="bcrypt 哈希成本因子（rounds=2^cost）")
    # 服务间调用密钥（case→agent 内部触发、connector 鉴权）；prod 必填
    service_api_key: str = Field(default="", description="服务间调用密钥，prod 必须配置")
    # 可信代理：True 时才信任 X-Forwarded-For / X-Real-IP（部署在 Nginx 后且由 compose/网关固定时开启）
    trust_proxy_headers: bool = Field(default=False, description="仅当部署在可信反代之后才开启")

    # ---- CORS ----
    cors_origins: str = "http://localhost:5173,http://localhost:3000,http://localhost:8080"

    # ---- 服务端口 ----
    auth_service_port: int = 8001
    case_service_port: int = 8002
    evidence_service_port: int = 8003
    agent_service_port: int = 8004
    audit_service_port: int = 8005
    lms_connector_port: int = 8011
    bms_connector_port: int = 8012
    ems_connector_port: int = 8013
    mes_connector_port: int = 8014

    # ---- 服务发现（Docker Compose 内服务名）----
    auth_service_url: str = "http://auth-service:8001"
    case_service_url: str = "http://case-service:8002"
    evidence_service_url: str = "http://evidence-service:8003"
    agent_service_url: str = "http://agent-service:8004"
    audit_service_url: str = "http://audit-service:8005"
    lms_connector_url: str = "http://lms-connector:8011"
    bms_connector_url: str = "http://bms-connector:8012"
    ems_connector_url: str = "http://ems-connector:8013"
    mes_connector_url: str = "http://mes-connector:8014"

    # ---- GxP 合规 ----
    audit_log_immutable: bool = True
    session_timeout_minutes: int = 15
    require_dual_signature: bool = True
    retention_years: int = 10

    # ---- LLM (Phase 2 启用) ----
    # provider 支持：glm (智谱) | qwen (阿里通义) | openai | anthropic | azure | "" (禁用)
    llm_provider: str = ""
    llm_api_key: str = ""
    llm_model: str = "claude-sonnet-4-20250514"
    llm_base_url: str = ""  # 自定义 endpoint，留空则按 provider 选择默认值
    llm_max_tokens: int = 2048
    llm_max_tokens_drafter: int = 4096  # Drafter 生成整篇 ICH Q9 报告需要更长输出
    llm_temperature: float = 0.2  # Reasoner 推理温度，偏低保证可复现性
    llm_timeout_seconds: int = 30  # HTTP 超时
    llm_max_retries: int = 2  # 失败重试次数（指数退避）

    # ---- LLM 多 Agent 博弈（Phase 2 改进5） ----
    # 对抗 Reasoner 使用的 secondary LLM，留空则降级到单 Reasoner
    llm_debate_provider: str = ""  # 对抗方 provider（如 qwen / openai）
    llm_debate_api_key: str = ""
    llm_debate_model: str = ""  # 留空则使用该 provider 的 default_model
    llm_debate_temperature: float = 0.5  # 对抗方温度略高，鼓励多样性

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
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


# 生产环境禁止的默认值 —— 忘记注入环境变量时 fail-fast，而不是带着公开密钥上线
_INSECURE_DEFAULTS = {
    "jwt_secret": "dev-secret-change-in-prod",
    "postgres_password": "rca-secret",
}


@lru_cache
def get_settings() -> Settings:
    """单例配置 —— prod 环境对危险默认值做防呆校验"""
    s = Settings()
    if s.env == "prod":
        problems = []
        for field, insecure in _INSECURE_DEFAULTS.items():
            if getattr(s, field) == insecure:
                problems.append(
                    f"{field} 仍为公开的默认值，禁止以 prod 环境启动（请注入环境变量 {field.upper()}）"
                )
        if not s.service_api_key:
            problems.append("service_api_key 未配置（服务间调用与 connector 鉴权依赖它）")
        if problems:
            raise RuntimeError("生产配置校验失败：\n" + "\n".join(problems))
    return s


settings = get_settings()
