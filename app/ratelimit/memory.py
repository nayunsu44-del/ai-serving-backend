from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass

from app.ratelimit.base import RateLimitBackend, RateLimitResult


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


class InMemoryRateLimit(RateLimitBackend):
    """Single-process token bucket rate limiter.

    A limit of 0 disables rate limiting.
    """

    def __init__(self, requests_per_minute: int) -> None:
        self.requests_per_minute = requests_per_minute
        self.capacity = float(max(requests_per_minute, 0))
        self.refill_per_second = self.capacity / 60.0 if self.capacity > 0 else 0.0
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()

    def _refill_bucket(self, key: str, now: float, *, create: bool) -> _Bucket | None:
        bucket = self._buckets.get(key)
        if bucket is None:
            if not create:
                return None
            bucket = _Bucket(tokens=self.capacity, updated_at=now)
            self._buckets[key] = bucket

        elapsed = now - bucket.updated_at
        bucket.tokens = min(self.capacity, bucket.tokens + elapsed * self.refill_per_second)
        bucket.updated_at = now
        return bucket

    def _result_for_bucket(self, bucket: _Bucket | None) -> RateLimitResult:
        if bucket is None or bucket.tokens >= 1.0:
            return RateLimitResult(
                allowed=True,
                remaining=math.inf if bucket is None else max(int(bucket.tokens), 0),
            )

        retry_after = (1.0 - bucket.tokens) / self.refill_per_second
        return RateLimitResult(
            allowed=False,
            retry_after_seconds=max(retry_after, 1.0),
            remaining=0,
        )

    async def preview(self, key: str) -> RateLimitResult:
        """Return whether a key has capacity without consuming a token."""

        if self.requests_per_minute <= 0:
            return RateLimitResult(allowed=True, remaining=math.inf)

        async with self._lock:
            bucket = self._refill_bucket(key, time.monotonic(), create=False)
            return self._result_for_bucket(bucket)

    async def allow(self, key: str) -> RateLimitResult:
        if self.requests_per_minute <= 0:
            return RateLimitResult(allowed=True, remaining=math.inf)

        async with self._lock:
            now = time.monotonic()
            bucket = self._refill_bucket(key, now, create=True)
            if bucket is None:
                return RateLimitResult(allowed=True, remaining=math.inf)

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return RateLimitResult(
                    allowed=True,
                    remaining=max(int(bucket.tokens), 0),
                )

            return self._result_for_bucket(bucket)
