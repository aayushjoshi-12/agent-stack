"""Pure event → Rich rendering helpers shared by the line REPL, plus the small text helpers
the TUI widgets use (``diff_kind``, ``tool_detail``, ``tool_args_lines``).

The REPL prints with Rich styles named after the terminal's own palette; the Textual TUI
styles through theme variables instead (``styles.tcss``, ``widgets.py``). These functions hold
no Textual state, so they unit-test without a running app.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from rich.text import Text

from agent_stack.core.events import AgentEvent

__all__ = [
    "assistant_block",
    "diff_kind",
    "failure_block",
    "reasoning_block",
    "subagent_block",
    "tool_args_lines",
    "tool_detail",
    "tool_output_text",
    "tool_started_block",
]

_META = "dim"  # secondary text (commands, ids, status words)
_THINK = "dim italic"  # the model's working, deliberately de-emphasised
_FAIL = "bold red"  # the one alarming tone: failures must stand out
_TOOL = "green"  # tool / subagent names (terminal's own green)

#: A line that marks the start of unified-diff output (git, patch, diff -u).
_DIFF_MARKERS = ("diff --git", "Index: ", "+++ ", "--- ")


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


def _detail(args: Any) -> str | None:
    """A short, useful summary for a tool call: its command, else its first short arg."""
    command = _command(args)
    if command:
        return command
    if isinstance(args, Mapping):
        for value in args.values():
            if isinstance(value, str) and len(value) < 40:
                return value
    return None


def tool_started_block(event: AgentEvent) -> Text:
    """A tool call as one REPL row: ``→ <tool> <command-or-arg>``."""
    line = Text("→ ")
    line.append(str(event.data.get("tool", "tool")), style=_TOOL)
    detail = _detail(event.data.get("args"))
    if detail:
        line.append("  ")
        line.append(detail)
    return line


def tool_output_text(event: AgentEvent) -> str:
    """The raw text chunk carried by a ``tool.output`` event."""
    return str(event.data.get("chunk", ""))


def diff_kind(line: str) -> str:
    """Classify one output line: ``meta`` (file headers), ``hunk``, ``add``, ``del``, ``ctx``."""
    if line.startswith(_DIFF_MARKERS):
        return "meta"
    if line.startswith("@@"):
        return "hunk"
    if line.startswith("+"):
        return "add"
    if line.startswith("-"):
        return "del"
    return "ctx"


def tool_args_lines(args: Any) -> str:
    """A call's arguments as ``key: value`` lines, for the expandable body of a card.

    ``run_command`` is excluded (its command is already the card's header and its output
    is the body). Long values are cut so one huge argument cannot flood the card.
    """
    if not isinstance(args, Mapping) or _command(args) is not None:
        return ""
    lines = []
    for key, value in args.items():
        text = " ".join(str(value).split())
        lines.append(f"{key}: {text if len(text) <= 200 else text[:199] + '…'}")
    return "\n".join(lines)


def tool_detail(args: Any) -> str:
    """The short header detail of a call: its command, else its first short argument."""
    return _detail(args) or ""


def subagent_block(event: AgentEvent, *, done: bool) -> Text:
    """A subagent lifecycle row, shown inline in the parent chat."""
    line = Text("→ ")
    line.append(str(event.data.get("agent_name", "subagent")), style=_TOOL)
    line.append("  done" if done else "  started", style=_META)
    return line


def failure_block(event: AgentEvent) -> Text:
    """A failed/cancelled run, the only line that ever shouts."""
    return Text(f"error: {event.data.get('error', 'run failed')}", style=_FAIL)
