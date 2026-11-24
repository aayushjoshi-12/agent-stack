"""``monitor_start`` / ``monitor_list`` / ``monitor_stop`` / ``monitor_notes``."""

from __future__ import annotations

from pathlib import Path

from langchain_core.tools import BaseTool, tool

from agent_stack.core.run_context import current_run_context
from agent_stack.monitor.service import MonitorError, MonitorService
from agent_stack.tools.builtins.terminal import outside_paths

__all__ = ["monitor_tools"]

_NO_RUN = "Error: monitors are only available inside a run."


def monitor_tools(
    service: MonitorService, workspace: Path, *, confine: bool = True
) -> tuple[BaseTool, ...]:
    def session() -> str | None:
        context = current_run_context()
        return None if context is None else context.session_id

    @tool
    async def monitor_start(
        command: str,
        mode: str = "change",
        pattern: str = "",
        interval_seconds: float = 30.0,
        max_minutes: float = 60.0,
    ) -> str:
        """Watch something in the background and be told only when it matters.

        Runs a shell command every ``interval_seconds`` (at least 5) with no model calls, and
        queues a note for you when: ``mode="change"`` the output changes (first poll is the
        baseline); ``mode="match"`` the output matches the regex ``pattern``; ``mode="exit"``
        the command succeeds (e.g. ``test -f build/done``, ``curl -sf localhost:8000/health``),
        which also ends the watch. Notes appear at the top of the next message you receive.
        A watch stops itself after ``max_minutes`` (default 60) or 10 notes. Use it to wait on
        a build, a deploy, a log or a file instead of polling by hand.
        """
        sid = session()
        if sid is None:
            return _NO_RUN
        if confine:
            outside = outside_paths(command, workspace)
            if outside:
                return f"Error: the command names paths outside the workspace ({outside[0]})."
        try:
            watch = service.start(
                sid,
                command,
                mode=mode,
                pattern=pattern or None,
                interval=interval_seconds,
                max_minutes=max_minutes,
            )
        except MonitorError as exc:
            return f"Error: {exc}"
        return f"Started {watch.describe()}"

    @tool
    async def monitor_list() -> str:
        """List this conversation's monitors with their status."""
        sid = session()
        if sid is None:
            return _NO_RUN
        watches = service.list(sid)
        return "\n".join(w.describe() for w in watches) if watches else "No monitors."

    @tool
    async def monitor_stop(id: str) -> str:
        """Stop a monitor by its id (from ``monitor_list``)."""
        sid = session()
        if sid is None:
            return _NO_RUN
        return f"Stopped {id}." if service.stop(sid, id) else f"No monitor {id}."

    @tool
    async def monitor_notes() -> str:
        """Read the monitor notes that are waiting now, without waiting for the next message."""
        sid = session()
        if sid is None:
            return _NO_RUN
        return service.drain(sid) or "No new monitor notes."

    return (monitor_start, monitor_list, monitor_stop, monitor_notes)
