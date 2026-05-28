from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass
class StreamLease:
    key: str
    limiter: StreamConcurrencyLimiter
    _released: bool = False

    async def release(self) -> None:
        await self.limiter.release(self)


class StreamConcurrencyLimiter:
    """Per-principal async stream semaphore registry."""

    def __init__(self, max_concurrent_streams: int) -> None:
        self.max_concurrent_streams = max_concurrent_streams
        self._semaphores: dict[str, asyncio.BoundedSemaphore] = {}
        self._active: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, key: str) -> StreamLease | None:
        async with self._lock:
            semaphore = self._semaphores.get(key)
            if semaphore is None:
                semaphore = asyncio.BoundedSemaphore(self.max_concurrent_streams)
                self._semaphores[key] = semaphore

            if semaphore.locked():
                return None

            await semaphore.acquire()
            self._active[key] = self._active.get(key, 0) + 1
            return StreamLease(key=key, limiter=self)

    async def release(self, lease: StreamLease) -> None:
        async with self._lock:
            if lease._released:
                return

            semaphore = self._semaphores.get(lease.key)
            active = self._active.get(lease.key, 0)
            if semaphore is None or active <= 0:
                lease._released = True
                return

            try:
                semaphore.release()
            except ValueError:
                lease._released = True
                return

            lease._released = True
            if active == 1:
                self._active.pop(lease.key, None)
                self._semaphores.pop(lease.key, None)
            else:
                self._active[lease.key] = active - 1
