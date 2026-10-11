"""The runtime event model.

Gateways consume :class:`AgentEvent` — a stable, small protocol — instead of raw
LangChain model payloads. Events are frozen values; per-run ordering lives in the
``sequence`` field assigned by the :class:`~agent_stack.core.event_bus.EventBus`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

__all__ = ["AgentEvent", "EventType"]


class EventType(StrEnum):
    """The closed set of events the runtime emits."""

    RUN_STARTED = "run.started"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    ASSISTANT_MESSAGE = "assistant.message"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"


@dataclass(frozen=True)
class AgentEvent:
    """An immutable runtime event.

    ``sequence`` is assigned by the event bus (monotonic per ``run_id``); a caller
    constructing an event directly may pass any placeholder, which the bus replaces.
    """

    id: str
    type: EventType
    run_id: str
    session_id: str
    sequence: int
    timestamp: datetime
    data: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        type: EventType,
        *,
        run_id: str,
        session_id: str,
        sequence: int,
        data: Mapping[str, Any] | None = None,
    ) -> AgentEvent:
        """Build an event with a fresh id and timestamp."""
        return cls(
            id=str(uuid4()),
            type=type,
            run_id=run_id,
            session_id=session_id,
            sequence=sequence,
            timestamp=datetime.now(UTC),
            data=dict(data) if data is not None else {},
        )
