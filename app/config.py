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


def parse_jwt_group_scope_map(entries: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for entry in entries:
        group, separator, scope = entry.partition("=")
        if not separator:
            continue
        group = group.strip()
        scope = scope.strip()
        if group and scope:
            mapping[group] = scope
    return mapping


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
    auth_mode: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["api_key"],
        alias="AUTH_MODE",
    )
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    api_key_last_used_min_interval_seconds: int = Field(
        default=60,
        ge=0,
        alias="API_KEY_LAST_USED_MIN_INTERVAL_SECONDS",
    )
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
    audit_store_messages: bool = Field(default=False, alias="AUDIT_STORE_MESSAGES")
    jwt_issuer: str | None = Field(default=None, alias="JWT_ISSUER")
    jwt_audience: str | None = Field(default=None, alias="JWT_AUDIENCE")
    jwt_jwks_url: str | None = Field(default=None, alias="JWT_JWKS_URL")
    jwt_algorithms: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["RS256"],
        alias="JWT_ALGORITHMS",
    )
    jwt_scope_claim: str = Field(default="groups", alias="JWT_SCOPE_CLAIM")
    jwt_group_scope_map: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        alias="JWT_GROUP_SCOPE_MAP",
    )
    jwt_org_claim: str = Field(default="org_id", alias="JWT_ORG_CLAIM")
    policy_mode: str = Field(default="log_only", alias="POLICY_MODE")
    forbidden_patterns: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        alias="FORBIDDEN_PATTERNS",
    )
    pii_masking_enabled: bool = Field(default=True, alias="PII_MASKING_ENABLED")
    pii_types: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["rrn", "card", "phone", "email"],
        alias="PII_TYPES",
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
        "auth_mode",
        "jwt_algorithms",
        "jwt_group_scope_map",
        "openai_models",
        "anthropic_models",
        "allowed_hosts",
        "trusted_proxies",
        "pii_types",
        "forbidden_patterns",
        mode="before",
    )
    @classmethod
    def parse_csv_lists(cls, value: Any) -> list[str]:
        return _parse_csv(value)

    @field_validator("auth_mode")
    @classmethod
    def validate_auth_mode(cls, value: list[str]) -> list[str]:
        allowed = {"api_key", "jwt"}
        unknown = [item for item in value if item not in allowed]
        if unknown:
            raise ValueError("AUTH_MODE must contain only: api_key, jwt")
        return value

    @field_validator("policy_mode")
    @classmethod
    def validate_policy_mode(cls, value: str) -> str:
        allowed = {"block", "log_only", "disabled"}
        if value not in allowed:
            raise ValueError("POLICY_MODE must be one of: block, log_only, disabled")
        return value

    def discard_raw_api_keys(self) -> None:
        """Drop service bearer keys after startup hashing."""

        self.api_keys = []
