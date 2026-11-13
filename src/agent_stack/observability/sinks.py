"""Fan one event out to several bus sinks (audit log, structured log, metrics)."""

from __future__ import annotations

from agent_stack.core.event_bus import EventAuditSink
from agent_stack.core.events import AgentEvent

__all__ = ["FanoutSink"]


class FanoutSink:
    """Write each event to every sink; a failing sink never stops the others or the run."""

    def __init__(self, *sinks: EventAuditSink) -> None:
        self._sinks = sinks

    def write(self, event: AgentEvent) -> None:
        for sink in self._sinks:
            try:
                sink.write(event)
            except Exception:
                continue
