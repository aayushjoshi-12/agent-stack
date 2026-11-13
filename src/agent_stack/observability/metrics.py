"""In-process counters and timings, derived from the event stream (P27).

``Metrics`` is a bus sink: it watches the events the runtime already emits, so the runtime
carries no instrumentation code. Everything is in memory (a snapshot for tests, a TUI panel or
a future exporter); there is no network exporter.
"""

from __future__ import annotations

import time
from collections import Counter, defaultdict
from typing import Any

from agent_stack.core.events import AgentEvent, EventType

__all__ = ["Metrics"]

_TERMINAL = (EventType.RUN_COMPLETED, EventType.RUN_FAILED)


class Metrics:
    """Counters by name (and tool/status where useful) plus summed durations."""

    def __init__(self) -> None:
        self.counters: Counter[str] = Counter()
        self.timings: dict[str, list[float]] = defaultdict(list)
        self._run_started: dict[str, float] = {}
        self._tool_started: dict[str, float] = {}

    def write(self, event: AgentEvent) -> None:
        now = time.monotonic()
        kind, data = event.type, event.data
        if kind is EventType.RUN_STARTED:
            self.counters["runs_started"] += 1
            self._run_started[event.run_id] = now
        elif kind in _TERMINAL:
            name = "runs_completed" if kind is EventType.RUN_COMPLETED else "runs_failed"
            self.counters[name] += 1
            started = self._run_started.pop(event.run_id, None)
            if started is not None:
                self.timings["run_seconds"].append(now - started)
        elif kind is EventType.TOOL_STARTED:
            self.counters[f"tool_calls.{data.get('tool')}"] += 1
            self._tool_started[str(data.get("tool_call_id"))] = now
        elif kind is EventType.TOOL_COMPLETED:
            if data.get("ok") is False:
                self.counters[f"tool_failures.{data.get('tool')}"] += 1
            started = self._tool_started.pop(str(data.get("tool_call_id")), None)
            if started is not None:
                self.timings["tool_seconds"].append(now - started)
        elif kind is EventType.APPROVAL_REQUIRED:
            self.counters["approvals_required"] += 1
        elif kind is EventType.APPROVAL_RESOLVED:
            self.counters[f"approvals_{data.get('status')}"] += 1

    def snapshot(self) -> dict[str, Any]:
        """Counters plus count/total/max for every timing, as plain data."""
        return {
            "counters": dict(self.counters),
            "timings": {
                name: {"count": len(v), "total": sum(v), "max": max(v)}
                for name, v in self.timings.items()
                if v
            },
        }
