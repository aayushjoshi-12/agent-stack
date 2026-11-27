"""A per-key token bucket, in process: what keeps a buggy client off the API and the model bill.

Each key (a principal, a chat) holds up to ``rate`` tokens and earns ``rate`` back per ``per``
seconds, so a human's burst passes and a loop is stopped. ``rate=None`` turns it off.
"""

from __future__ import annotations

import time
from collections.abc import Callable


class RateLimiter:
    def __init__(
        self, rate: int | None, per: float = 60.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._rate = rate
        self._per = per
        self._clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, at)

    def take(self, key: str) -> float:
        """Spend a token: 0.0 when allowed, else the seconds until one is back (nothing spent)."""
        if self._rate is None:
            return 0.0
        now = self._clock()
        tokens, at = self._buckets.get(key, (float(self._rate), now))
        tokens = min(float(self._rate), tokens + (now - at) * self._rate / self._per)
        if tokens >= 1.0:
            self._buckets[key] = (tokens - 1.0, now)
            return 0.0
        self._buckets[key] = (tokens, now)
        return (1.0 - tokens) * self._per / self._rate
