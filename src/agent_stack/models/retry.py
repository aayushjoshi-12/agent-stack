"""Which model errors are worth retrying, and how long the provider asked us to wait (P51)."""

from __future__ import annotations

from collections.abc import Iterator

import httpx

#: Request timeout, conflict, rate limit; every 5xx is added in ``transient``.
_RETRY_STATUS = frozenset({408, 409, 429})


def _chain(exc: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__  # explicit "raise from" only: an implicit context is unrelated


def _status(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    return status if isinstance(status, int) else None


def transient(exc: BaseException) -> bool:
    """True for a rate limit, a server error, a timeout or a dropped connection.

    Auth, bad request, unknown model and context-too-long are not: asking again gets the same.
    """
    for error in _chain(exc):
        status = _status(error)
        if status is not None:
            return status in _RETRY_STATUS or status >= 500
        if isinstance(error, (httpx.TransportError, ConnectionError, TimeoutError)):
            return True
    return False


def retry_after(exc: BaseException) -> float | None:
    """Seconds the provider asked to wait (``retry-after-ms`` or ``retry-after``), if it said."""
    for error in _chain(exc):
        headers = getattr(error, "headers", None)
        if headers is None:
            headers = getattr(getattr(error, "response", None), "headers", None)
        if headers is None:
            continue
        for name, scale in (("retry-after-ms", 1000.0), ("retry-after", 1.0)):
            raw = headers.get(name)
            if raw is None:
                continue
            try:
                return max(0.0, float(raw) / scale)
            except (TypeError, ValueError):
                continue  # shortcut: an HTTP-date Retry-After is ignored (backoff is used)
    return None
