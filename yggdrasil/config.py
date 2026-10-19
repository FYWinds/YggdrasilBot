"""Deployment-level settings (environment variables only).

Everything a group admin may want to tune lives in the database instead and is
edited from the dashboard (see :mod:`yggdrasil.settings`).
"""

from functools import cached_property

from pydantic import Field, PostgresDsn, RedisDsn, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

_COMMON = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


class DbConfig(BaseSettings):
    model_config = _COMMON

    host: str = Field(default="127.0.0.1", alias="POSTGRES_HOST")
    port: int = Field(default=5432, alias="POSTGRES_PORT")
    user: str = Field(default="ygg", alias="POSTGRES_USER")
    password: str = Field(default="ygg", alias="POSTGRES_PASSWORD")
    database: str = Field(default="ygg", alias="POSTGRES_DB")
    pool_size: int = Field(default=10, alias="POSTGRES_POOL_SIZE")

    @computed_field
    @cached_property
    def dsn(self) -> str:
        return PostgresDsn.build(
            scheme="postgresql+asyncpg",
            username=self.user,
            password=self.password,
            host=self.host,
            port=self.port,
            path=self.database,
        ).encoded_string()


class RedisConfig(BaseSettings):
    model_config = _COMMON

    url: RedisDsn = Field(default=RedisDsn("redis://127.0.0.1:6379/0"), alias="REDIS_URL")


class LogConfig(BaseSettings):
    model_config = _COMMON

    level: str = Field(default="INFO", alias="LOG_LEVEL")
    json_output: bool = Field(default=False, alias="LOG_JSON")
    format: str = "<g>{time:MM-DD HH:mm:ss}</g> [<lvl>{level: <7}</lvl>] <c>{name}:{line}</c> | {message}{extra[trace]}"


class TelemetryConfig(BaseSettings):
    model_config = _COMMON

    otlp_endpoint: str | None = Field(default=None, alias="OTEL_EXPORTER_OTLP_ENDPOINT")
    sdk_disabled: bool = Field(default=False, alias="OTEL_SDK_DISABLED")

    @property
    def enabled(self) -> bool:
        return bool(self.otlp_endpoint) and not self.sdk_disabled


class AppConfig(BaseSettings):
    model_config = _COMMON

    environment: str = Field(default="dev", alias="YGG_ENV")
    host: str = Field(default="0.0.0.0", alias="YGG_HOST")
    port: int = Field(default=8080, alias="YGG_PORT")
    onebot_access_token: str = Field(default="", alias="ONEBOT_ACCESS_TOKEN")
    # Fernet key (urlsafe base64, 32 bytes) used to encrypt provider API keys at rest.
    secret_key: str = Field(alias="YGG_SECRET_KEY")
    # Bootstrap admins; the dashboard manages the rest.
    bootstrap_superusers: list[int] = Field(default_factory=list, alias="YGG_SUPERUSERS")
    bootstrap_admin_emails: list[str] = Field(default_factory=list, alias="YGG_ADMIN_EMAILS")
    # Response header oauth2-proxy (set-xauthrequest) returns to Traefik forwardAuth; dev mode trusts
    # YGG_DEV_ADMIN_EMAIL instead.
    auth_email_header: str = Field(default="X-Auth-Request-Email", alias="YGG_AUTH_EMAIL_HEADER")
    dev_admin_email: str | None = Field(default=None, alias="YGG_DEV_ADMIN_EMAIL")
    # Playwright server for read_url's browser fallback (the render service exposes one on :3000).
    browser_ws_url: str | None = Field(default=None, alias="YGG_BROWSER_WS_URL")
    # Render service for the render_* tools; unset disables them.
    render_url: str | None = Field(default=None, alias="YGG_RENDER_URL")
    web_dist: str = Field(default="web/dist", alias="YGG_WEB_DIST")
    # Extra headers for outbound LLM calls; Cloudflare in front of new-api rejects default SDK user agents.
    llm_user_agent: str = Field(default="yggdrasil-bot/0.1", alias="YGG_LLM_USER_AGENT")
    # Dashboard links invocations to their Tempo trace in this Grafana (no links when unset).
    grafana_url: str | None = Field(default=None, alias="YGG_GRAFANA_URL")
    # Sandbox workspaces (yggdrasil.sandbox); disabled while the shared sandboxd token is unset.
    sandbox_token: str | None = Field(default=None, alias="YGG_SANDBOX_TOKEN")
    sandbox_namespace: str = Field(default="yggdrasil", alias="YGG_SANDBOX_NAMESPACE")
    sandbox_warmpool: str = Field(default="yggdrasil-sandbox", alias="YGG_SANDBOX_WARMPOOL")
    # Public links to services served from a workspace are https://<random id>.<this domain>; unset disables them.
    sandbox_link_domain: str | None = Field(default=None, alias="YGG_SANDBOX_LINK_DOMAIN")
    # LLM relay for agents inside workspaces (yggdrasil.relay): the base URL sandboxes reach it at; unset disables
    # it. The bot process serves it on its own port so only that port has to be opened to sandbox pods.
    relay_url: str | None = Field(default=None, alias="YGG_RELAY_URL")
    relay_port: int = Field(default=8090, alias="YGG_RELAY_PORT")


DB_CONFIG = DbConfig()
REDIS_CONFIG = RedisConfig()
LOG_CONFIG = LogConfig()
TELEMETRY_CONFIG = TelemetryConfig()
APP_CONFIG = AppConfig()  # pyright: ignore[reportCallIssue]
