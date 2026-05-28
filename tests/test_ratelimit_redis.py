from __future__ import annotations

import math

import pytest

pytest.importorskip("fakeredis")

from fakeredis import aioredis

from app.ratelimit.redis import RedisTokenBucketRateLimit


@pytest.mark.asyncio
async def test_redis_rate_limit_allows_capacity_then_denies() -> None:
    now = 1_000.0
    client = aioredis.FakeRedis()
    limiter = RedisTokenBucketRateLimit(client, 3, now_fn=lambda: now)

    results = [await limiter.allow("api-key") for _ in range(4)]

    assert [result.allowed for result in results] == [True, True, True, False]
    assert [result.remaining for result in results[:3]] == [2, 1, 0]
    assert results[3].remaining == 0
    assert 0 < results[3].retry_after_seconds <= 60

    await limiter.aclose()


@pytest.mark.asyncio
async def test_redis_rate_limit_keys_are_independent() -> None:
    now = 1_000.0
    client = aioredis.FakeRedis()
    limiter = RedisTokenBucketRateLimit(client, 1, now_fn=lambda: now)

    first_a = await limiter.allow("a")
    first_b = await limiter.allow("b")
    second_a = await limiter.allow("a")

    assert first_a.allowed is True
    assert first_b.allowed is True
    assert second_a.allowed is False

    await limiter.aclose()


@pytest.mark.asyncio
async def test_redis_rate_limit_refills_after_time_advances() -> None:
    now = 1_000.0
    client = aioredis.FakeRedis()
    limiter = RedisTokenBucketRateLimit(client, 3, now_fn=lambda: now)

    for _ in range(3):
        assert (await limiter.allow("api-key")).allowed is True
    assert (await limiter.allow("api-key")).allowed is False

    now += 20.0
    refilled = await limiter.allow("api-key")

    assert refilled.allowed is True
    assert refilled.remaining == 0

    await limiter.aclose()


@pytest.mark.asyncio
async def test_redis_rate_limit_zero_rpm_is_disabled() -> None:
    client = aioredis.FakeRedis()
    limiter = RedisTokenBucketRateLimit(client, 0, now_fn=lambda: 1_000.0)

    results = [await limiter.allow("api-key") for _ in range(3)]

    assert all(result.allowed for result in results)
    assert all(result.remaining == math.inf for result in results)
    assert await client.exists("rl:api-key") == 0

    await limiter.aclose()


@pytest.mark.asyncio
async def test_redis_preview_is_non_consuming() -> None:
    client = aioredis.FakeRedis()
    limiter = RedisTokenBucketRateLimit(client, 3, now_fn=lambda: 1_000.0)

    previews = [await limiter.preview("api-key") for _ in range(5)]
    consumed = await limiter.allow("api-key")

    assert all(result.allowed for result in previews)
    assert [result.remaining for result in previews] == [3, 3, 3, 3, 3]
    assert consumed.allowed is True
    assert consumed.remaining == 2

    await limiter.aclose()
