from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RunStatus(StrEnum):
    """Lifecycle states of a single agent run."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Run:
    """A single execution of an agent against a session.

    ``Run`` owns the execution *lifecycle*; it never owns conversation content.
    """

    id: str
    session_id: str
    status: RunStatus = RunStatus.PENDING
    step_count: int = 0
    tool_call_count: int = 0
    error: str | None = None
