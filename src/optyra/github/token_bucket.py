"""Async token bucket pacing search-API usage (report 01prd §7: stay <= ~20 req/min,
hard ceiling 30/min authenticated). Injectable clock + sleep make it deterministic in tests.
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable


class TokenBucket:
    def __init__(
        self,
        rate_per_minute: float,
        capacity: float | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], object] = asyncio.sleep,
    ) -> None:
        self.rate = rate_per_minute / 60.0
        self.capacity = capacity if capacity is not None else max(1.0, rate_per_minute / 60.0 * 5)
        self._tokens = self.capacity
        self._updated = clock()
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._updated)
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
        self._updated = now

    def wait_time(self) -> float:
        if self._tokens >= 1:
            return 0.0
        return (1.0 - self._tokens) / self.rate

    async def acquire(self) -> None:
        """Wait until one token is available."""
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                wait = self.wait_time()
            await self._sleep(wait)
