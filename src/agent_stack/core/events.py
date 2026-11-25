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
    # What one model call consumed: tokens and, when the provider reports it, the exact cost in
    # USD. Emitted after every model response (including subagents'); never terminal.
    MODEL_USAGE = "model.usage"
    # The model's thinking/reasoning for a step, when the provider returns any. Emitted
    # just before the ASSISTANT_MESSAGE it belongs to; not terminal for any stream.
    MODEL_REASONING = "model.reasoning"
    TOOL_STARTED = "tool.started"
    # Live tool output (P14). Not terminal for any stream: a command may emit many before
    # its TOOL_COMPLETED.
    TOOL_OUTPUT = "tool.output"
    TOOL_COMPLETED = "tool.completed"
    # Human-in-the-loop (P26): a gated tool call is waiting for a decision / it was decided.
    # Neither is terminal; the run continues (or rejects the call) once resolved.
    APPROVAL_REQUIRED = "approval.required"
    APPROVAL_RESOLVED = "approval.resolved"
    # Auto mode (the advisor answered a gated call itself): data has tool, tool_call_id,
    # verdict (allow/deny/ask) and a one-line reason. ``ask`` is followed by the usual
    # ``approval.required`` for the human. Not terminal.
    APPROVAL_AUTO = "approval.auto"
    # Subagent lifecycle, published on the *parent's* run scope (Phase 20). Neither is
    # terminal for any stream — a child finishing must never end its parent's stream.
    SUBAGENT_STARTED = "subagent.started"
    SUBAGENT_COMPLETED = "subagent.completed"
    # A conversation was forked (P30). Published on the *parent* session; ``run_id`` is the
    # new branch's session id. Data: parent_session_id, branch_point_event_id,
    # branch_session_id. Not terminal for any stream.
    SESSION_BRANCHED = "session.branched"


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
    parent_run_id: str | None = None

    @classmethod
    def create(
        cls,
        type: EventType,
        *,
        run_id: str,
        session_id: str,
        sequence: int,
        data: Mapping[str, Any] | None = None,
        parent_run_id: str | None = None,
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
            parent_run_id=parent_run_id,
        )
