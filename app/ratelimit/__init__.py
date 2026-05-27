"""Rate limiting backends."""

from __future__ import annotations

from app.config import Settings
from app.ratelimit.base import RateLimitBackend
from app.ratelimit.memory import InMemoryRateLimit


def build_rate_limit_backend(settings: Settings) -> RateLimitBackend:
    backend = settings.rate_limit_backend.lower()
    if backend == "memory":
        return InMemoryRateLimit(settings.rate_limit_rpm)

    # TODO: Add a shared production backend such as Redis here so rate limits
    # apply consistently across workers and hosts.
    raise ValueError(f"Unsupported RATE_LIMIT_BACKEND: {settings.rate_limit_backend}")
