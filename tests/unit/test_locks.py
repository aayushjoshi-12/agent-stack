"""Unit tests for per-session locking.

Two runs on the *same* session must serialize; runs on *different* sessions must not.
"""

from __future__ import annotations

import asyncio

from agent_stack.core.locks import SessionLockManager


async def test_concurrent_same_session_requests_serialize() -> None:
    locks = SessionLockManager()
    active = 0
    max_active = 0

    async def worker() -> None:
        nonlocal active, max_active
        async with locks.lock("s1"):
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(*(worker() for _ in range(5)))

    assert max_active == 1, "same-session workers overlapped"


async def test_different_sessions_run_concurrently() -> None:
    locks = SessionLockManager()
    active = 0
    max_active = 0

    async def worker(session_id: str) -> None:
        nonlocal active, max_active
        async with locks.lock(session_id):
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(*(worker(f"s{i}") for i in range(5)))

    assert max_active == 5, "different-session workers should never block each other"


async def test_reentrant_same_session_serializes_sequentially() -> None:
    locks = SessionLockManager()
    order: list[int] = []

    async def worker(value: int) -> None:
        async with locks.lock("s1"):
            order.append(value)
            await asyncio.sleep(0.005)

    await asyncio.gather(*[worker(value) for value in (1, 2, 3)])

    assert order == [1, 2, 3], "same-session work must be strictly sequential"
