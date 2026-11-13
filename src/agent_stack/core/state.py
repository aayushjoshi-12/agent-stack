"""Run-state records: definitions that describe a run's progress.

``Checkpoint`` is a durable, semantic snapshot of run state at a loop boundary
(Phase 10). SQLite is the index, so a checkpoint carries only counters and a status —
never conversation content, which lives in the transcript files. ``AgentState``
(full mutable state) arrives with a later phase; the loop today keeps its counters
as locals.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = ["Checkpoint", "CheckpointStatus", "TodoItem", "TodoStatus"]


class CheckpointStatus(StrEnum):
    """The loop boundary a checkpoint was taken at."""

    AI_MESSAGE = "ai_message"
    TOOL_BATCH_COMPLETED = "tool_batch_completed"
    RUN_COMPLETED = "run_completed"


@dataclass(frozen=True)
class Checkpoint:
    """A semantic snapshot of run state (never conversation content)."""

    run_id: str
    session_id: str
    step: int
    tool_call_count: int
    status: CheckpointStatus
    created_at: str


class TodoStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"


@dataclass(frozen=True)
class TodoItem:
    """One entry of a session's task list; ``id`` is stable and unique within the session."""

    id: int
    text: str
    status: TodoStatus = TodoStatus.PENDING
