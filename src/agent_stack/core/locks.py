"""Per-session serialization for the runtime.

A :class:`SessionLockManager` owns one ``asyncio.Lock`` per ``session_id``, so two
concurrent runs against the same session are serialized while runs on different sessions
proceed in parallel. ``asyncio.Lock`` is already an async context manager, so the lock
method returns it directly:

    async with locks.lock("s1"):
        ...
"""

from __future__ import annotations

import asyncio

__all__ = ["SessionLockManager"]


class SessionLockManager:
    """One ``asyncio.Lock`` per session id, created on first use."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def lock(self, session_id: str) -> asyncio.Lock:
        """Return the per-session lock, creating it on first use."""
        return self._locks.setdefault(session_id, asyncio.Lock())
