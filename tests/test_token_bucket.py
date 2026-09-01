"""Token bucket pacing tests (fake clock, deterministic)."""

from __future__ import annotations

import asyncio

from optyra.github.token_bucket import TokenBucket


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def test_bucket_allows_burst_up_to_capacity():
    clock = FakeClock()
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        clock.now += seconds
        sleeps.append(seconds)

    bucket = TokenBucket(60, capacity=3, clock=clock, sleep=sleep)  # 1 token/sec
    for _ in range(3):
        await bucket.acquire()
    assert sleeps == []  # burst consumed the initial capacity


async def test_bucket_paces_beyond_capacity():
    clock = FakeClock()
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        clock.now += seconds
        sleeps.append(seconds)

    bucket = TokenBucket(60, capacity=1, clock=clock, sleep=sleep)  # 1 token/sec
    await bucket.acquire()  # instant
    t0 = clock.now
    await bucket.acquire()
    await bucket.acquire()
    assert clock.now - t0 == 2.0  # one second per extra token
    assert sleeps == [1.0, 1.0]


async def test_bucket_refills_over_time():
    clock = FakeClock()
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        clock.now += seconds
        sleeps.append(seconds)

    bucket = TokenBucket(60, capacity=1, clock=clock, sleep=sleep)
    await bucket.acquire()
    clock.now += 5  # 5 tokens accrue (capped at capacity=1)
    await bucket.acquire()
    assert sleeps == []


async def test_concurrent_acquire_stays_under_rate():
    clock = FakeClock()
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        clock.now += seconds
        sleeps.append(seconds)

    bucket = TokenBucket(120, capacity=1, clock=clock, sleep=sleep)  # 2 tokens/sec
    await asyncio.gather(*(bucket.acquire() for _ in range(5)))
    # 4 extra tokens at 2/sec = 2.0s minimum total wait
    assert clock.now >= 2.0
