"""Pure event → Rich rendering helpers for the TUI gateway.

Styling is *semantic, not decorative*: content inherits the terminal's default
foreground, and emphasis uses attributes (bold, dim, italic) plus a small set of
ANSI accents that resolve to the terminal's own palette (no hex/RGB literals).
These functions hold no Textual state, so they unit-test without a running app and
are reused by the line REPL.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from rich.text import Text

from agent_stack.core.events import AgentEvent, EventType

__all__ = [
    "assistant_block",
    "event_block",
    "failure_block",
    "human_block",
    "reasoning_block",
    "subagent_block",
    "tool_output_text",
    "tool_started_block",
    "welcome_block",
]

_META = "dim"  # secondary text (commands, ids, status words)
_THINK = "dim italic"  # the model's working, deliberately de-emphasised
_FAIL = "bold red"  # the one alarming tone: failures must stand out
_ACCENT = "cyan"  # headings / banner (terminal's own cyan)
_TOOL = "green"  # tool names (terminal's own green)

#: Figlet "small" wordmark for the empty-chat banner.
WELCOME_ART = r"""
                   _          _           _
 __ _ __ _ ___ _ _| |_ ___ __| |_ __ _ __| |__
/ _` / _` / -_) ' \  _|___(_-<  _/ _` / _| / /
\__,_\__, \___|_||_\__|   /__/\__\__,_\__|_\_\
     |___/
""".strip("\n")


def welcome_block() -> Text:
    """The empty-conversation banner: an ASCII wordmark plus a one-line hint."""
    line = Text(WELCOME_ART, style=_ACCENT)
    line.append("\n\n  a terminal agent runtime", style=_META)
    line.append("\n  type a message, or /agent <name> <task> to delegate", style=_META)
    return line


def human_block(text: str) -> Text:
    """The user's own message (the panel around it is CSS, see ``.msg-user``)."""
    return Text(text)


def assistant_block(event: AgentEvent) -> Text:
    """An assistant message; its tool calls are announced by their own rows."""
    return Text(str(event.data.get("content", "")))


def reasoning_block(event: AgentEvent) -> Text:
    """The model's thinking for a step, rendered dim/italic."""
    return Text(str(event.data.get("text", "")), style=_THINK)


def _command(args: Any) -> str | None:
    """The command string carried by a ``run_command`` call, if any."""
    if not isinstance(args, Mapping):
        return None
    raw = args.get("command")
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    if isinstance(raw, (list, tuple)):
        return " ".join(str(part) for part in raw)
    return str(raw)


def tool_started_block(event: AgentEvent) -> Text:
    """A tool call: the tool name, plus the command for ``run_command``."""
    line = Text("  ")
    line.append(str(event.data.get("tool", "tool")), style=_TOOL)
    command = _command(event.data.get("args"))
    if command:
        line.append("  ")
        line.append(command, style=_META)
    return line


def tool_output_text(event: AgentEvent) -> str:
    """The raw text chunk carried by a ``tool.output`` event."""
    return str(event.data.get("chunk", ""))


def subagent_block(event: AgentEvent, *, done: bool) -> Text:
    """A subagent lifecycle row, shown inline in the parent chat."""
    line = Text("  ")
    line.append(str(event.data.get("agent_name", "subagent")), style=_TOOL)
    line.append("  done" if done else "  started", style=_META)
    return line


def failure_block(event: AgentEvent) -> Text:
    """A failed/cancelled run, the only line that ever shouts."""
    return Text(f"error: {event.data.get('error', 'run failed')}", style=_FAIL)


def event_block(event: AgentEvent) -> Text:
    """Render any child-run event with the same blocks the chat log uses."""
    if event.type is EventType.MODEL_REASONING:
        return reasoning_block(event)
    if event.type is EventType.ASSISTANT_MESSAGE:
        return assistant_block(event)
    if event.type is EventType.TOOL_STARTED:
        return tool_started_block(event)
    if event.type is EventType.TOOL_OUTPUT:
        return Text(tool_output_text(event), style=_META)
    if event.type is EventType.RUN_FAILED:
        return failure_block(event)
    return Text(event.type.value, style=_META)
