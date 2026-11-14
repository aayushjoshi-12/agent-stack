"""In-process publish/subscribe for runtime events.

The bus owns runtime event *distribution*: it assigns the monotonic per-run
``sequence`` and fans events out to subscribers. Every published event also goes to an
optional audit sink (P8b) from this same call — the one choke point, so nothing can be
emitted unlogged. Durable replay is a later phase; rendering is a gateway's job.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Protocol

from agent_stack.core.events import AgentEvent, EventType

__all__ = ["EventAuditSink", "EventBus", "InMemoryEventBus"]

_TERMINAL_EVENTS = frozenset({EventType.RUN_COMPLETED, EventType.RUN_FAILED})


class EventAuditSink(Protocol):
    """Optional sink the bus writes each sequenced event to (the audit log)."""

    def write(self, event: AgentEvent) -> None: ...


class EventBus(Protocol):
    """The infrastructure boundary for runtime event distribution."""

    async def publish(self, event: AgentEvent) -> None: ...

    def subscribe(self, run_id: str) -> AsyncIterator[AgentEvent]: ...


class InMemoryEventBus:
    """In-process pub/sub with one queue per subscriber and one counter per run.

    ``publish`` assigns a monotonic per-run ``sequence`` (starting at ``0``), writes the
    sequenced event to the optional ``audit`` sink, and fans the event out to every
    current subscriber of that run. ``subscribe`` is an async generator yielding events
    in publish order; it terminates after a terminal event (``RUN_COMPLETED`` or
    ``RUN_FAILED``) has been yielded. Subscribing before any publish is fine — a
    subscriber's queue is created on first use.
    """

    def __init__(self, audit: EventAuditSink | None = None) -> None:
        self._subscribers: dict[str, list[asyncio.Queue[AgentEvent]]] = {}
        self._sequences: dict[str, int] = {}
        self._audit = audit

    async def publish(self, event: AgentEvent) -> None:
        """Sequence the event for its run, audit it, then deliver it to subscribers."""
        sequence = self._sequences.get(event.run_id, 0)
        self._sequences[event.run_id] = sequence + 1
        sequenced = replace(event, sequence=sequence)
        await self._persist(sequenced)
        if self._audit is not None:
            self._audit.write(sequenced)
        for queue in self._subscribers.get(event.run_id, []):
            queue.put_nowait(sequenced)

    async def _persist(self, event: AgentEvent) -> None:
        """Hook for durable subclasses; runs before subscribers see the event."""

    async def subscribe(self, run_id: str) -> AsyncIterator[AgentEvent]:
        """Yield a run's events in publish order until a terminal event is seen."""
        queue: asyncio.Queue[AgentEvent] = asyncio.Queue()
        self._subscribers.setdefault(run_id, []).append(queue)
        try:
            while True:
                event = await queue.get()
                yield event
                if event.type in _TERMINAL_EVENTS:
                    return
        finally:
            subscribers = self._subscribers.get(run_id)
            if subscribers is not None and queue in subscribers:
                subscribers.remove(queue)
