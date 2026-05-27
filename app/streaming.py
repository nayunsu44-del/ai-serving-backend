from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass
class StreamLease:
    key: str
    semaphore: asyncio.BoundedSemaphore

    async def release(self) -> None:
        self.semaphore.release()


class StreamConcurrencyLimiter:
    """Per-principal async stream semaphore registry."""

    def __init__(self, max_concurrent_streams: int) -> None:
        self.max_concurrent_streams = max_concurrent_streams
        self._semaphores: dict[str, asyncio.BoundedSemaphore] = {}
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
            return StreamLease(key=key, semaphore=semaphore)
