"""Per-call tool output streaming that tools read through a :class:`ContextVar`.

A tool that produces live output (``run_command``) emits ``(stream, chunk)`` pairs to the
sink returned by :func:`current_tool_output_sink` while it runs. ``ToolExecutor`` installs
a per-call sink; the runtime supplies one that publishes ``tool.output`` events. Tools
import nothing from the application and behave unchanged when no sink is installed (the
default outside a run) — mirroring :mod:`agent_stack.core.run_context`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

#: Receives one decoded chunk of tool output and the stream it came from.
ToolOutputSink = Callable[[str, str], Awaitable[None]]

__all__ = ["ToolOutputSink", "current_tool_output_sink", "tool_output_sink"]


_current: ContextVar[ToolOutputSink | None] = ContextVar(
    "agent_stack_tool_output_sink", default=None
)


def current_tool_output_sink() -> ToolOutputSink | None:
    """The current call's output sink, or ``None`` outside a tool call."""
    return _current.get()


@contextmanager
def tool_output_sink(sink: ToolOutputSink) -> Iterator[None]:
    """Make ``sink`` current for the duration of the ``with`` block."""
    token = _current.set(sink)
    try:
        yield
    finally:
        _current.reset(token)
