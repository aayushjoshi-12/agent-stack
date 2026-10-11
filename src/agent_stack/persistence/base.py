"""Store protocols: the infrastructure boundaries for durable records.

Thin on purpose: canonical definitions live next to their domain (``core``). This module
only declares the infrastructure protocols that span implementations; callers import the
protocol from here and the implementation from ``persistence.sqlite`` / ``memory``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from agent_stack.core.run import Run
from agent_stack.core.state import Checkpoint
from agent_stack.tools.executor import ToolExecutionResult

__all__ = ["CheckpointStore", "RunStore"]


@runtime_checkable
class RunStore(Protocol):
    """Infrastructure boundary for run lifecycle records."""

    async def save(self, run: Run) -> None: ...

    async def get(self, run_id: str) -> Run | None: ...

    async def list_incomplete(self) -> list[Run]:
        """Return runs still pending or running (startup recovery dropdown)."""
        ...


@runtime_checkable
class CheckpointStore(Protocol):
    """Infrastructure boundary for durable run checkpoints and tool records.

    Checkpoints and tool-execution rows are run *state* metadata only — never
    conversation content (SQLite is the index; the transcript files are the truth).
    """

    async def save(self, checkpoint: Checkpoint) -> None: ...

    async def get_latest(self, run_id: str) -> Checkpoint | None: ...

    async def record_tool_executions(
        self, run_id: str, results: Sequence[ToolExecutionResult]
    ) -> None:
        """Persist a completed tool batch in a single transaction.

        ``(run_id, tool_call_id)`` is the idempotency key: recording the same batch
        again is a no-op.
        """
        ...

    async def completed_tool_call_ids(self, run_id: str) -> set[str]:
        """Every ``tool_call_id`` already recorded for a run (the idempotency set)."""
        ...
