"""Store protocols: the infrastructure boundaries for durable records.

Thin on purpose: canonical definitions live next to their domain (``core``). This module
only declares the infrastructure protocols that span implementations; callers import the
protocol from here and the implementation from ``persistence.sqlite`` / ``memory``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent_stack.core.run import Run

__all__ = ["RunStore"]


@runtime_checkable
class RunStore(Protocol):
    """Infrastructure boundary for run lifecycle records."""

    async def save(self, run: Run) -> None: ...

    async def get(self, run_id: str) -> Run | None: ...

    async def list_incomplete(self) -> list[Run]:
        """Return runs still pending or running (startup recovery dropdown)."""
        ...
