"""In-memory persistence implementations.

Volatile by design: used by tests and by producers that want zero disk I/O. Reads return
copies so callers can never mutate the store's internal state in place.
"""

from __future__ import annotations

from dataclasses import replace

from agent_stack.core.run import Run, RunStatus

__all__ = ["InMemoryRunStore"]


class InMemoryRunStore:
    """A volatile :class:`agent_stack.persistence.base.RunStore`."""

    def __init__(self) -> None:
        self._runs: dict[str, Run] = {}

    async def save(self, run: Run) -> None:
        self._runs[run.id] = replace(run)

    async def get(self, run_id: str) -> Run | None:
        run = self._runs.get(run_id)
        return replace(run) if run is not None else None

    async def list_incomplete(self) -> list[Run]:
        """Return copies of runs still pending or running (recovery dropdown)."""
        incomplete = (
            run
            for run in self._runs.values()
            if run.status in (RunStatus.PENDING, RunStatus.RUNNING)
        )
        return [replace(run) for run in incomplete]
