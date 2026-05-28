"""Rate limiting backends."""

from __future__ import annotations

import logging

from app.config import Settings
from app.ratelimit.base import RateLimitBackend
from app.ratelimit.memory import InMemoryRateLimit


logger = logging.getLogger(__name__)


def build_rate_limit_backend(
    settings: Settings,
    *,
    requests_per_minute: int | None = None,
    key_prefix: str | None = None,
) -> RateLimitBackend:
    rpm = settings.rate_limit_rpm if requests_per_minute is None else requests_per_minute
    backend = settings.rate_limit_backend.lower()
    if backend == "memory":
        return InMemoryRateLimit(rpm)

    if backend == "redis":
        if not settings.redis_url:
            if settings.rate_limit_strict:
                raise ValueError(
                    "REDIS_URL is required when RATE_LIMIT_BACKEND=redis "
                    "and RATE_LIMIT_STRICT=true"
                )
            logger.warning(
                "RATE_LIMIT_BACKEND=redis requested but REDIS_URL is not set; "
                "falling back to in-memory rate limiting"
            )
            return InMemoryRateLimit(rpm)

        try:
            from app.ratelimit.redis import from_url

            return from_url(
                settings.redis_url,
                rpm,
                key_prefix="rl:" if key_prefix is None else key_prefix,
            )
        except Exception as exc:
            if settings.rate_limit_strict:
                raise
            logger.warning(
                "Failed to initialize Redis rate limiter; falling back to memory",
                extra={"extra_fields": {"exception_class": exc.__class__.__name__}},
            )
            return InMemoryRateLimit(rpm)

    raise ValueError(f"Unsupported RATE_LIMIT_BACKEND: {settings.rate_limit_backend}")
