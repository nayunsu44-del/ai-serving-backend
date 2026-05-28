from __future__ import annotations

import inspect
import math
import time
from collections.abc import Callable
from typing import Any

from redis.asyncio import Redis

from app.ratelimit.base import RateLimitBackend, RateLimitResult


class RedisTokenBucketRateLimit(RateLimitBackend):
    """Redis-backed token bucket rate limiter.

    Refill and consume happen in one Lua script so multiple workers share the
    same atomic decision for a given key.
    """

    _ALLOW_SCRIPT = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_per_second = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local ttl_seconds = tonumber(ARGV[4])

local bucket = redis.call("HMGET", key, "tokens", "ts")
local tokens = tonumber(bucket[1])
local ts = tonumber(bucket[2])

if tokens == nil or ts == nil then
    tokens = capacity
    ts = now
end

local elapsed = now - ts
if elapsed < 0 then
    elapsed = 0
end

tokens = tokens + (elapsed * refill_per_second)
if tokens > capacity then
    tokens = capacity
end

local allowed = 0
local retry_after = 0

if tokens >= 1 then
    allowed = 1
    tokens = tokens - 1
else
    retry_after = (1 - tokens) / refill_per_second
end

redis.call("HSET", key, "tokens", tostring(tokens), "ts", tostring(now))
redis.call("EXPIRE", key, ttl_seconds)

return {tostring(allowed), tostring(math.floor(tokens)), tostring(retry_after)}
"""

    _PREVIEW_SCRIPT = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_per_second = tonumber(ARGV[2])
local now = tonumber(ARGV[3])

local bucket = redis.call("HMGET", key, "tokens", "ts")
local tokens = tonumber(bucket[1])
local ts = tonumber(bucket[2])

if tokens == nil or ts == nil then
    tokens = capacity
    ts = now
end

local elapsed = now - ts
if elapsed < 0 then
    elapsed = 0
end

tokens = tokens + (elapsed * refill_per_second)
if tokens > capacity then
    tokens = capacity
end

local allowed = 0
local retry_after = 0

if tokens >= 1 then
    allowed = 1
else
    retry_after = (1 - tokens) / refill_per_second
end

return {tostring(allowed), tostring(math.floor(tokens)), tostring(retry_after)}
"""

    def __init__(
        self,
        client: Redis,
        requests_per_minute: int,
        *,
        refill_per_second: float | None = None,
        key_prefix: str = "rl:",
        now_fn: Callable[[], float] = time.time,
        ttl_seconds: int = 5 * 60,
    ) -> None:
        self.client = client
        self.requests_per_minute = requests_per_minute
        self.capacity = float(max(requests_per_minute, 0))
        self.refill_per_second = (
            float(refill_per_second)
            if refill_per_second is not None
            else self.capacity / 60.0 if self.capacity > 0 else 0.0
        )
        self.key_prefix = key_prefix
        self.now_fn = now_fn
        self.ttl_seconds = ttl_seconds

    async def allow(self, key: str) -> RateLimitResult:
        if self.requests_per_minute <= 0:
            return RateLimitResult(allowed=True, remaining=math.inf)

        result = await self.client.eval(
            self._ALLOW_SCRIPT,
            1,
            f"{self.key_prefix}{key}",
            str(self.capacity),
            str(self.refill_per_second),
            str(float(self.now_fn())),
            str(int(self.ttl_seconds)),
        )
        allowed, remaining, retry_after = self._parse_script_result(result)
        return RateLimitResult(
            allowed=allowed,
            remaining=remaining,
            retry_after_seconds=0.0 if allowed else max(retry_after, 0.0),
        )

    async def preview(self, key: str) -> RateLimitResult:
        if self.requests_per_minute <= 0:
            return RateLimitResult(allowed=True, remaining=math.inf)

        result = await self.client.eval(
            self._PREVIEW_SCRIPT,
            1,
            f"{self.key_prefix}{key}",
            str(self.capacity),
            str(self.refill_per_second),
            str(float(self.now_fn())),
        )
        allowed, remaining, retry_after = self._parse_script_result(result)
        return RateLimitResult(
            allowed=allowed,
            remaining=remaining,
            retry_after_seconds=0.0 if allowed else max(retry_after, 0.0),
        )

    async def aclose(self) -> None:
        close = getattr(self.client, "aclose", None) or getattr(self.client, "close", None)
        if close is None:
            return

        result = close()
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def _parse_script_result(result: Any) -> tuple[bool, int, float]:
        values = [_decode_script_value(item) for item in result]
        return values[0] == "1", max(int(float(values[1])), 0), float(values[2])


def _decode_script_value(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def from_url(
    url: str,
    requests_per_minute: int,
    key_prefix: str = "rl:",
) -> RedisTokenBucketRateLimit:
    client = Redis.from_url(url, decode_responses=False)
    return RedisTokenBucketRateLimit(
        client,
        requests_per_minute,
        key_prefix=key_prefix,
    )
