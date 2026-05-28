from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass

from fastapi import Depends, Request

from app.auth import APIKeyPrincipal, require_api_key
from app.errors import RateLimitError


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    retry_after_seconds: float = 0.0
    remaining: int | float = 0


class RateLimitBackend(ABC):
    """Abstract rate limit backend.

    The in-memory implementation is appropriate for tests and single-process
    development only. Production deployments should use a shared backend such
    as Redis so limits are enforced across workers and hosts.
    """

    requests_per_minute: int

    @abstractmethod
    async def allow(self, key: str) -> RateLimitResult:
        """Return whether the caller identified by key may proceed."""

    @abstractmethod
    async def preview(self, key: str) -> RateLimitResult:
        """Return whether the caller identified by key has capacity without consuming it."""


def get_rate_limit_backend(request: Request) -> RateLimitBackend:
    return request.app.state.rate_limiter


async def enforce_rate_limit(
    principal: APIKeyPrincipal = Depends(require_api_key),
    backend: RateLimitBackend = Depends(get_rate_limit_backend),
) -> APIKeyPrincipal:
    result = await backend.allow(principal.api_key_hash)
    if result.allowed:
        return principal

    retry_after = max(1, math.ceil(result.retry_after_seconds))
    raise RateLimitError(
        "Rate limit exceeded",
        headers={"Retry-After": str(retry_after)},
    )
