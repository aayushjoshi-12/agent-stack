"""Conversation rows: what a run leaves in a :class:`~tabs.Conversation`.

Message rows (:class:`UserMessage`, :class:`AgentMessage`, :class:`Notice`), the
:class:`SubagentRow`, and the collapsible :class:`Disclosure` card used for tool calls and
thinking. They hold no runtime state beyond their own presentation; the app routes events into
them (``runs.py``). Colours come from theme variables (``[$accent]`` markup and the
stylesheet), never literals, so every theme restyles them.
"""

from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Sequence
from typing import Any, ClassVar

from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.markup import escape
from textual.message import Message
from textual.widgets import Markdown, Static

from agent_stack.gateways.tui.render import diff_kind

__all__ = [
    "AgentMessage",
    "Disclosure",
    "Notice",
    "SubagentRow",
    "UserMessage",
    "diff_rows",
    "format_duration",
    "spinner_frame",
]

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
#: Lines of shell output kept per card; older lines are dropped (and counted).
MAX_LINES = 400
#: Lines shown while a command runs, and when the user opens a finished card.
TAIL_RUNNING = 10
TAIL_OPEN = 60
#: Finished cards with at most this many output lines stay open.
SHORT_OUTPUT = 6
#: Diff rows shown in a card; the files screen (ctrl+g) has the whole diff.
DIFF_ROWS = 80
_HUNK = re.compile(r"@@ -(\d+)(?:,\d+)? \+(\d+)")
#: How built-in tools are named on screen; any other tool shows its own name.
TOOL_NAMES = {
    "read_file": "Read",
    "write_file": "Write",
    "edit_file": "Edit",
    "run_command": "Run",
    "grep_files": "Search",
    "search_files": "Find",
    "list_directory": "List",
    "read_artifact": "Read output",
    "web_fetch": "Fetch",
    "web_search": "Web search",
    "web_screenshot": "Screenshot",
    "delegate": "Delegate",
    "remember": "Remember",
    "recall": "Recall",
    "forget": "Forget",
}
#: The bracket that hangs a card's result under its header.
_ELBOW = "[$foreground-muted]⎿[/]  "


def diff_rows(lines: Sequence[str], width: int = 0, *, limit: int | None = None) -> list[str]:
    """A unified diff as numbered rows; added and removed lines on a tinted band ``width`` wide.

    File headers are dropped and a gap between hunks is a ``⋮``; past ``limit`` rows the rest
    is counted. Used by tool cards and the changed-files screen.
    """
    rows: list[str] = []
    old = new = shown = 0
    for line in lines:
        kind = diff_kind(line)
        if kind == "meta":
            continue
        if kind == "hunk":
            if numbers := _HUNK.match(line):
                old, new = int(numbers[1]), int(numbers[2])
            if shown:
                rows.append("[$foreground-muted]     ⋮[/]")
            continue
        if limit is not None and shown == limit:
            rows.append("[$foreground-muted]… more lines in the files screen (ctrl+g)[/]")
            break
        shown += 1
        text = line[1:] if line[:1] in ("+", "-", " ") else line
        if kind == "add":
            number, sign, style = new, "+", "$foreground on $success 15%"
            new += 1
        elif kind == "del":
            number, sign, style = old, "-", "$foreground on $error 15%"
            old += 1
        else:
            number, sign, style = new, " ", "$foreground-muted"
            old, new = old + 1, new + 1
        rows.append(f"[{style}]{escape(f'{number:>4} {sign} {text}'.ljust(width))}[/]")
    return rows


def _elbowed(rows: list[str]) -> str:
    """Hang ``rows`` under the header: an elbow on the first, the rest aligned with it."""
    return "\n".join((_ELBOW if i == 0 else "   ") + row for i, row in enumerate(rows))


def format_duration(seconds: float) -> str:
    """``0.4s`` / ``12s`` / ``1m05s``: short, fixed-meaning durations for status text."""
    if seconds < 10:
        return f"{seconds:.1f}s"
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes}m{rest:02d}s"


def spinner_frame(now: float) -> str:
    return SPINNER[int(now * 10) % len(SPINNER)]


# ── conversation rows ────────────────────────────────────────────────────────
class UserMessage(Static):
    """What the user typed: a raised panel with an accent bar."""

    def __init__(self, text: str, *, queued: bool = False, images: Sequence[str] = ()) -> None:
        marks = "\n".join(f"[dim]\\[image: {escape(name)}][/]" for name in images)
        shown = "\n".join(part for part in (escape(text), marks) if part) + (
            "\n[dim]queued: read at the agent's next step · up arrow to edit or cancel[/]"
            if queued
            else ""
        )
        super().__init__(shown, classes="user-message -queued" if queued else "user-message")
        self.text = text


class AgentMessage(Markdown):
    """An assistant reply, rendered as Markdown."""

    def __init__(self, content: str) -> None:
        super().__init__(content, classes="agent-message")
        self._text = content

    def grow(self, text: str) -> None:
        """Add streamed text (P45). Queued on the widget, so it never runs before the mount."""
        self._text += text
        self.call_later(self._catch_up)

    def settle(self, content: str) -> None:
        """Show the step's final text (it replaces whatever was streamed)."""
        self._text = content
        self.call_later(self.update, content)

    def _catch_up(self) -> None:
        if self._text.startswith(self.source) and (missing := self._text[len(self.source) :]):
            self.append(missing)


class Notice(Static):
    """A failed run's message, on an error-coloured bar."""

    def __init__(self, text: str) -> None:
        super().__init__(f"✗ {escape(text)}", classes="notice")


class SubagentRow(Static, can_focus=True):
    """One subagent's lifecycle, updated in place (running → done); Enter or a click opens it."""

    BINDINGS: ClassVar[list[Any]] = [Binding("enter", "open", "open", show=False)]

    class Open(Message):
        """Show this subagent's own run (the app pushes a ``SubagentScreen``)."""

        def __init__(self, run_id: str, agent: str, task: str) -> None:
            super().__init__()
            self.run_id = run_id
            self.agent = agent
            self.brief = task

    def __init__(self, name: str, *, run_id: str = "", task: str = "", done: bool = False) -> None:
        self.agent_name = name
        self.run_id = run_id
        self.brief = task
        self.outcome = "completed" if done else "running"
        self._stored = done  # from a reopened conversation: its duration is unknown
        self._t0 = time.monotonic()
        self._elapsed = 0.0
        super().__init__(classes="subagent-row")

    def on_mount(self) -> None:
        self._paint(self._t0)

    @property
    def running(self) -> bool:
        return self.outcome == "running"

    def finish(self, status: str) -> None:
        self.outcome = status
        self._elapsed = time.monotonic() - self._t0
        self._paint(time.monotonic())

    def on_click(self) -> None:
        self.action_open()

    def action_open(self) -> None:
        if self.run_id:
            self.post_message(self.Open(self.run_id, self.agent_name, self.brief))

    def tick(self, now: float) -> None:
        if self.running:
            self._paint(now)

    def _paint(self, now: float) -> None:
        name = escape(self.agent_name)
        if self.running:
            glyph = f"[$accent]{spinner_frame(now)}[/]"
            meta = f"running {format_duration(now - self._t0)}"
        elif self.outcome == "completed":
            took = "" if self._stored else f" in {format_duration(self._elapsed)}"
            glyph, meta = "[$success]●[/]", f"done{took}"
        else:
            glyph = "[$error]●[/]"
            meta = f"{escape(self.outcome)} after {format_duration(self._elapsed)}"
        task = " ".join(self.brief.split())
        task = f" {escape(task[:70])}{'...' if len(task) > 70 else ''}" if task else ""
        hint = " · enter to open" if self.run_id else ""
        self.update(
            f"{glyph} [bold]Subagent[/]([$foreground-muted]{name}[/])[$foreground-muted]{task}[/]"
            f"\n  [$foreground-muted]⎿  {meta}{hint}[/]"
        )


# ── collapsible cards (tool calls, thinking) ─────────────────────────────────
class DisclosureHeader(Horizontal, can_focus=True):
    """The clickable, keyboard-focusable title row of a :class:`Disclosure`."""

    BINDINGS: ClassVar[list[Any]] = [
        Binding("enter", "toggle_card", "expand/collapse", show=False),
        Binding("space", "toggle_card", "expand/collapse", show=False),
    ]

    def compose(self):  # type: ignore[no-untyped-def]
        yield Static("", classes="dh-main")
        yield Static("", classes="dh-meta")

    def on_click(self) -> None:
        self.action_toggle_card()

    def action_toggle_card(self) -> None:
        card = self.parent
        if isinstance(card, Disclosure):
            card.toggle()


class Disclosure(Vertical):
    """A card with a one-line header and an optional body that expands.

    ``kind="tool"`` cards track a command's life (spinner → ✓/✗, duration) and hold its
    capped output; ``kind="think"`` cards hold the model's reasoning. Nothing here talks
    to the runtime: the app calls :meth:`append`, :meth:`finish` and :meth:`tick`.
    """

    def __init__(
        self,
        kind: str,
        label: str,
        detail: str = "",
        *,
        body: str = "",
        opened: bool = False,
        running: bool = False,
    ) -> None:
        super().__init__(classes=f"disclosure -{kind}")
        self.kind = kind
        self.label = label
        self.detail = detail
        self.status = "running" if running else "static"
        self.diff = False
        self._t0 = time.monotonic()
        self._elapsed = 0.0
        self._lines: deque[str] = deque(maxlen=MAX_LINES)
        self._partial = ""
        self._dropped = 0
        self._stale = False
        self._user_toggled = False
        self._is_open = opened
        self._ready = False
        self._output_seen = False  # streamed output, as opposed to the static body
        self._pin_open = False  # a diff stays open when the card settles
        if body:
            self._ingest(body, final=True)

    # -- composition --------------------------------------------------------
    def compose(self):  # type: ignore[no-untyped-def]
        yield DisclosureHeader(classes="dh-header")
        yield Static("", classes="dh-body")

    def on_mount(self) -> None:
        self._ready = True  # `is_mounted` is still False inside our own Mount handler
        self._sync()

    # -- state --------------------------------------------------------------
    @property
    def has_body(self) -> bool:
        return bool(self._lines or self._partial)

    @property
    def needs_tick(self) -> bool:
        return self.status == "running" or self._stale

    @property
    def line_count(self) -> int:
        return len(self._lines) + self._dropped

    def toggle(self) -> None:
        if not self.has_body:
            return
        self._user_toggled = True
        self._is_open = not self._is_open
        self._sync()

    def append(self, chunk: str) -> None:
        """Add raw output; held until a full line arrives (see :meth:`_ingest`)."""
        self._output_seen = True
        self._ingest(chunk, final=False)

    def finish(self, status: str) -> None:
        """Settle the card: ``done`` or ``failed``; tidy the open state."""
        if self._partial:
            self._ingest("", final=True)
        self.status = status
        self._elapsed = time.monotonic() - self._t0
        if not self._user_toggled:
            # Short streamed output and failures stay visible; long output folds away,
            # and a card that only carries its arguments stays closed.
            self._is_open = (
                status == "failed"
                or self._pin_open
                or (self._output_seen and self.line_count <= SHORT_OUTPUT)
            )
        self._stale = True
        self._sync()

    def show_diff(self, diff: str) -> None:
        """Replace the card's body (its arguments) with the unified diff of what the call did.

        The card opens so the change is visible; if the reader already toggled it, their choice
        wins. Called for ``write_file``/``edit_file`` once the change has been recorded.
        """
        self._lines.clear()
        self._partial = ""
        self._dropped = 0
        self.diff = False
        self._output_seen = True
        self._pin_open = True
        self._ingest(diff, final=True)
        if not self._user_toggled:
            self._is_open = True
        self._sync()

    def await_approval(self) -> None:
        """Show that the call is paused for a human's decision."""
        self.status = "waiting"
        self._stale = True
        self._sync()

    def resume_running(self) -> None:
        """The call was approved and is executing."""
        self.status = "running"
        self._stale = True
        self._sync()

    def tick(self, now: float) -> None:
        if not self.needs_tick or not self._ready:
            return
        self._stale = False
        self._sync(now)

    # -- internals ----------------------------------------------------------
    def _ingest(self, chunk: str, *, final: bool) -> None:
        text = self._partial + chunk
        *complete, self._partial = text.split("\n")
        if final and self._partial:
            complete.append(self._partial)
            self._partial = ""
        for line in complete:
            if len(self._lines) == MAX_LINES:
                self._dropped += 1
            self._lines.append(line)
            if not self.diff and diff_kind(line) in ("meta", "hunk"):
                self.diff = True
        self._stale = True

    def _glyph(self, now: float) -> str:
        if self.kind == "think":
            return "[$foreground-muted]∴[/]"
        if self.status == "running":
            return f"[$accent]{spinner_frame(now)}[/]"
        colour = {"waiting": "$warning", "failed": "$error", "done": "$success"}
        return f"[{colour.get(self.status, '$foreground-muted')}]●[/]"

    def _sync(self, now: float | None = None) -> None:
        if not self._ready:
            return
        now = time.monotonic() if now is None else now
        body = self._body_markup() if self._is_open and self.has_body else self._summary()
        self.set_class(self._is_open and self.has_body, "-open")
        self.set_class(bool(body), "-body")
        self.set_class(self.status == "failed", "-failed")
        if self.kind == "think":
            main = f"{self._glyph(now)} [italic $foreground-muted]{escape(self.label)}[/]"
        else:
            name = escape(TOOL_NAMES.get(self.label, self.label))
            detail = f"([$foreground-muted]{escape(self.detail)}[/])" if self.detail else ""
            main = f"{self._glyph(now)} [bold]{name}[/]{detail}"
        if self.status == "waiting":
            meta = "[$warning]awaiting approval[/]"
        elif self.status == "running":
            meta = f"[$foreground-muted]{format_duration(now - self._t0)}[/]"
        elif self.status == "failed":  # never colour alone: the word says it too
            meta = f"[$error]failed[/] [$foreground-muted]{format_duration(self._elapsed)}[/]"
        elif self.status == "done" and self._elapsed >= 1:
            meta = f"[$foreground-muted]{format_duration(self._elapsed)}[/]"
        else:
            meta = ""
        try:
            self.query_one(".dh-main", Static).update(main)
            self.query_one(".dh-meta", Static).update(meta)
            self.query_one(".dh-body", Static).update(body)
        except NoMatches:
            return  # torn down between the tick and now

    def on_resize(self) -> None:
        self._sync()  # diff rows are padded to the width

    def _summary(self) -> str:
        """What a closed card shows under its header: nothing, or one line of what is inside."""
        if not self.has_body or self.kind == "think" or not self._output_seen:
            return ""
        if self.diff:
            return f"{_ELBOW}{self._diff_title()}  [$foreground-muted]enter to view[/]"
        lines = [line for line in self._lines if line.strip()] or [self._partial]
        more = self.line_count - 1
        rest = f"  [$foreground-muted]… +{more} lines (enter to expand)[/]" if more > 0 else ""
        return f"{_ELBOW}[$foreground-muted]{escape(lines[0][:200])}[/]{rest}"

    def _body_markup(self) -> str:
        if self.diff:
            return self._diff_markup()
        limit = TAIL_RUNNING if self.status == "running" else TAIL_OPEN
        lines = list(self._lines)
        hidden = self._dropped
        if len(lines) > limit:
            hidden += len(lines) - limit
            lines = lines[-limit:]
        style = "italic $foreground-muted" if self.kind == "think" else "$foreground-muted"
        out: list[str] = []
        if hidden:
            out.append(f"[$foreground-muted italic]… {hidden} earlier lines hidden[/]")
        out += [f"[{style}]{escape(line) or ' '}[/]" for line in lines]
        return _elbowed(out)

    def _diff_title(self) -> str:
        path = next((ln[6:] for ln in self._lines if ln.startswith("+++ b/")), self.detail)
        added = sum(1 for ln in self._lines if diff_kind(ln) == "add")
        removed = sum(1 for ln in self._lines if diff_kind(ln) == "del")
        verb = "Created" if any(ln.startswith("--- /dev/null") for ln in self._lines) else "Updated"
        return f"{verb} [bold]{escape(path)}[/]  [$success]+{added}[/] [$error]-{removed}[/]"

    def _diff_markup(self) -> str:
        width = max(0, self.size.width - 8)
        rows = [self._diff_title(), *diff_rows(list(self._lines), width, limit=DIFF_ROWS)]
        return _elbowed(rows)
