"""Cooperative cancellation for agent runs.

A :class:`CancellationToken` is a plain flag the runtime polls between loop iterations
and around tool execution. Cancelling is *cooperative*: the flag alone stops nothing;
the run must reach a check point (``raise_if_cancelled``) or be interrupted directly.

Methods are intentionally **not** thread-safe — asyncio guarantees a single thread runs
this coroutine, so the flag only needs to be safe across event-loop interleavings.
"""

from __future__ import annotations

from agent_stack.core.errors import RunCancelled

__all__ = ["CancellationToken"]


class CancellationToken:
    """A cooperative cancellation flag shared between a canceller and a run."""

    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        """Request cancellation. Idempotent and never raises."""
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        """Whether cancellation has been requested."""
        return self._cancelled

    def raise_if_cancelled(self) -> None:
        """Raise :class:`RunCancelled` when cancellation was requested."""
        if self._cancelled:
            raise RunCancelled("run cancelled")
