"""Pure event → Rich rendering helpers for the TUI gateway.

These functions turn a single :class:`AgentEvent` (or an audit record) into a Rich
renderable for the chat/subagent/shells/audit panes. They hold no Textual state, so
they unit-test without a running app and are reused by the line REPL.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from rich.text import Text

from agent_stack.core.events import AgentEvent, EventType

__all__ = [
    "assistant_block",
    "audit_line",
    "event_block",
    "header_status",
    "human_block",
    "run_status_block",
    "subagent_block",
    "tool_completed_block",
    "tool_output_text",
    "tool_started_block",
]

_HUMAN = "bold cyan"
_ASSISTANT = "bold green"
_TOOL = "yellow"
_OK = "green"
_ERR = "red"
_DIM = "dim"
_SUB = "magenta"


def human_block(text: str) -> Text:
    """The user's own message, as it appears in the chat log."""
    line = Text()
    line.append("▸ you   ", style=_HUMAN)
    line.append(text)
    return line


def assistant_block(event: AgentEvent) -> Text:
    """An assistant message plus the names of any tool calls it requested."""
    line = Text()
    line.append("▸ asst  ", style=_ASSISTANT)
    line.append(str(event.data.get("content", "")))
    for name in event.data.get("tool_calls") or ():
        line.append("\n  ⚙ ", style=_TOOL)
        line.append(str(name), style=_TOOL)
    return line


def tool_started_block(event: AgentEvent) -> Text:
    """An in-progress tool status line (finalized by :func:`tool_completed_block`)."""
    line = Text()
    line.append("  ⚙ ", style=_TOOL)
    line.append(str(event.data.get("tool", "tool")), style=_TOOL)
    line.append("  ⏳", style=_DIM)
    return line


def tool_completed_block(event: AgentEvent) -> Text:
    """A finished tool status line."""
    line = Text()
    line.append("  ✓ ", style=_OK)
    line.append(str(event.data.get("tool", "tool")), style=_OK)
    return line


def tool_output_text(event: AgentEvent) -> str:
    """The raw text chunk carried by a ``tool.output`` event."""
    return str(event.data.get("chunk", ""))


def subagent_block(event: AgentEvent, *, done: bool) -> Text:
    """A subagent lifecycle line, shown inline in the parent chat."""
    line = Text()
    line.append("  ↳ ", style=_SUB)
    line.append(str(event.data.get("agent_name", "subagent")), style=_SUB)
    line.append(" done" if done else " started", style=_DIM)
    return line


def run_status_block(event: AgentEvent) -> Text:
    """A terminal run line (completed or failed/cancelled)."""
    line = Text()
    if event.type is EventType.RUN_COMPLETED:
        line.append("● run completed", style=_OK)
    else:
        line.append(f"● {event.data.get('error', 'run failed')}", style=_ERR)
    return line


def event_block(event: AgentEvent) -> Text:
    """Render any child-run event with the same blocks the chat log uses."""
    if event.type is EventType.ASSISTANT_MESSAGE:
        return assistant_block(event)
    if event.type is EventType.TOOL_STARTED:
        return tool_started_block(event)
    if event.type is EventType.TOOL_COMPLETED:
        return tool_completed_block(event)
    if event.type is EventType.TOOL_OUTPUT:
        return Text(tool_output_text(event), style=_DIM)
    if event.type in (EventType.RUN_COMPLETED, EventType.RUN_FAILED):
        return run_status_block(event)
    return Text(event.type.value, style=_DIM)


def audit_line(record: Mapping[str, Any]) -> Text:
    """One audit JSONL record as a compact single line."""
    line = Text()
    line.append(str(record.get("sequence", "?")), style=_DIM)
    line.append(" ")
    line.append(str(record.get("type", "")), style=_TOOL)
    data = record.get("data")
    if data:
        line.append(" ")
        line.append(json.dumps(data, ensure_ascii=False, default=str), style=_DIM)
    return line


def header_status(
    *,
    agent: str,
    session: str,
    model: str,
    running: bool,
    frame: str,
    tools: int,
) -> str:
    """The one-line header status string."""
    state = f"{frame} running" if running else "○ idle"
    return f"{agent} · session={session} · {model} · {state} · tools={tools}"
