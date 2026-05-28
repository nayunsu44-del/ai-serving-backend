from __future__ import annotations

import pytest

from app.config import Settings
from app.ratelimit import build_rate_limit_backend
from app.ratelimit.memory import InMemoryRateLimit


def test_redis_strict_missing_url_raises_at_startup() -> None:
    settings = Settings(
        rate_limit_backend="redis",
        redis_url=None,
        rate_limit_strict=True,
    )

    with pytest.raises(
        ValueError,
        match="REDIS_URL is required when RATE_LIMIT_BACKEND=redis and RATE_LIMIT_STRICT=true",
    ):
        build_rate_limit_backend(settings)


def test_redis_non_strict_missing_url_falls_back_to_memory() -> None:
    settings = Settings(
        rate_limit_backend="redis",
        redis_url=None,
        rate_limit_strict=False,
    )

    backend = build_rate_limit_backend(settings)

    assert isinstance(backend, InMemoryRateLimit)
