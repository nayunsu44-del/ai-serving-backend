from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _parse_csv(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return value


class Settings(BaseSettings):
    """Application settings loaded from environment variables or .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    api_keys: Annotated[list[str], NoDecode] = Field(default_factory=list, alias="API_KEYS")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    database_url: str = Field(
        default="sqlite+aiosqlite:///./data/app.db",
        alias="DATABASE_URL",
    )
    redis_url: str | None = Field(default=None, alias="REDIS_URL")
    audit_enabled: bool = Field(default=True, alias="AUDIT_ENABLED")
    audit_sync: bool = Field(default=False, alias="AUDIT_SYNC")
    audit_fallback_path: str = Field(
        default="./data/audit_fallback.jsonl",
        alias="AUDIT_FALLBACK_PATH",
    )

    rate_limit_rpm: int = Field(
        default=60,
        ge=0,
        alias="RATE_LIMIT_RPM",
        description="Requests per minute per API key; 0 disables rate limiting.",
    )
    rate_limit_backend: str = Field(default="memory", alias="RATE_LIMIT_BACKEND")
    rate_limit_strict: bool = Field(default=False, alias="RATE_LIMIT_STRICT")
    pre_auth_rpm_per_ip: int = Field(
        default=30,
        ge=0,
        alias="PRE_AUTH_RPM_PER_IP",
        description="Failed/missing auth attempts per minute per client IP; 0 disables.",
    )
    trusted_proxies: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        alias="TRUSTED_PROXIES",
    )
    trust_forwarded_for: bool = Field(default=False, alias="TRUST_FORWARDED_FOR")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    default_max_tokens: int = Field(default=1024, alias="DEFAULT_MAX_TOKENS")
    max_request_bytes: int = Field(default=1_048_576, ge=1, alias="MAX_REQUEST_BYTES")
    max_messages: int = Field(default=200, ge=1, alias="MAX_MESSAGES")
    max_message_chars: int = Field(default=100_000, ge=1, alias="MAX_MESSAGE_CHARS")
    max_model_name_chars: int = Field(default=128, ge=1, alias="MAX_MODEL_NAME_CHARS")
    max_output_tokens: int = Field(default=4096, ge=1, alias="MAX_OUTPUT_TOKENS")
    stream_max_duration_seconds: int = Field(
        default=300,
        ge=1,
        alias="STREAM_MAX_DURATION_SECONDS",
    )
    max_concurrent_streams_per_key: int = Field(
        default=4,
        ge=1,
        alias="MAX_CONCURRENT_STREAMS_PER_KEY",
    )
    docs_enabled: bool = Field(default=True, alias="DOCS_ENABLED")
    allowed_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["*"],
        alias="ALLOWED_HOSTS",
    )

    openai_models: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["gpt-4o", "gpt-4o-mini"],
        alias="OPENAI_MODELS",
    )
    anthropic_models: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["claude-sonnet-4-6", "claude-3-5-haiku-latest"],
        alias="ANTHROPIC_MODELS",
    )

    @field_validator(
        "api_keys",
        "openai_models",
        "anthropic_models",
        "allowed_hosts",
        "trusted_proxies",
        mode="before",
    )
    @classmethod
    def parse_csv_lists(cls, value: Any) -> list[str]:
        return _parse_csv(value)

    def discard_raw_api_keys(self) -> None:
        """Drop service bearer keys after startup hashing."""

        self.api_keys = []
