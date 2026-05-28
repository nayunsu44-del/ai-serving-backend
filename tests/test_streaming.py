from __future__ import annotations

import pytest

from app.streaming import StreamConcurrencyLimiter


@pytest.mark.asyncio
async def test_stream_limiter_cleans_up_idle_key_after_release() -> None:
    limiter = StreamConcurrencyLimiter(max_concurrent_streams=1)

    lease = await limiter.acquire("principal-a")
    assert lease is not None
    assert "principal-a" in limiter._semaphores

    await lease.release()

    assert "principal-a" not in limiter._semaphores
    assert "principal-a" not in limiter._active

    await lease.release()
    assert "principal-a" not in limiter._semaphores


@pytest.mark.asyncio
async def test_stream_limiter_rejects_at_capacity_and_reopens_after_release() -> None:
    limiter = StreamConcurrencyLimiter(max_concurrent_streams=1)

    first = await limiter.acquire("principal-a")
    assert first is not None
    assert await limiter.acquire("principal-a") is None

    await first.release()

    second = await limiter.acquire("principal-a")
    assert second is not None
    await second.release()

    assert "principal-a" not in limiter._semaphores
    assert "principal-a" not in limiter._active


@pytest.mark.asyncio
async def test_stream_limiter_tracks_different_keys_independently() -> None:
    limiter = StreamConcurrencyLimiter(max_concurrent_streams=1)

    first = await limiter.acquire("principal-a")
    second = await limiter.acquire("principal-b")
    assert first is not None
    assert second is not None
    assert await limiter.acquire("principal-a") is None
    assert await limiter.acquire("principal-b") is None

    await first.release()
    assert "principal-a" not in limiter._semaphores
    assert "principal-b" in limiter._semaphores
    third = await limiter.acquire("principal-a")
    assert third is not None

    await second.release()
    assert "principal-b" not in limiter._semaphores
    await third.release()
    assert "principal-a" not in limiter._semaphores
