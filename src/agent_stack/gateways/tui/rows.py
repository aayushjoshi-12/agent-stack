"""Conversation rows: what a run leaves in a :class:`~tabs.Conversation`.

Message rows (:class:`UserMessage`, :class:`AgentMessage`, :class:`Notice`), the
:class:`SubagentRow`, and the collapsible :class:`Disclosure` card used for tool calls and
thinking. They hold no runtime state beyond their own presentation; the app routes events into
them (``runs.py``). Colours come from theme variables (``[$accent]`` markup and the
stylesheet), never literals, so every theme restyles them.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any, ClassVar

from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.markup import escape
from textual.widgets import Markdown, Static

from agent_stack.gateways.tui.render import diff_kind

__all__ = [
    "DIFF_MARKUP",
    "AgentMessage",
    "Disclosure",
    "Notice",
    "SubagentRow",
    "UserMessage",
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

#: Markup per unified-diff line kind (``render.diff_kind``), for cards and the changes screen.
DIFF_MARKUP = {
    "meta": "bold",
    "hunk": "$accent",
    "add": "$success",
    "del": "$error",
    "ctx": "$foreground-muted",
}


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

    def __init__(self, text: str, *, queued: bool = False) -> None:
        shown = escape(text) + (
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


class SubagentRow(Static):
    """One subagent's lifecycle, updated in place (running → done)."""

    def __init__(self, name: str) -> None:
        self.agent_name = name
        self.outcome = "running"
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

    def tick(self, now: float) -> None:
        if self.running:
            self._paint(now)

    def _paint(self, now: float) -> None:
        name = escape(self.agent_name)
        if self.running:
            glyph = f"[$accent]{spinner_frame(now)}[/]"
            meta = f"running {format_duration(now - self._t0)}"
        elif self.outcome == "completed":
            glyph, meta = "[$success]✓[/]", f"done {format_duration(self._elapsed)}"
        else:
            glyph, meta = "[$error]✗[/]", f"{escape(self.outcome)} {format_duration(self._elapsed)}"
        self.update(f"  ⤷ {glyph} [bold]{name}[/] [$foreground-muted]subagent · {meta}[/]")


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
        if self.status == "running":
            return f"[$accent]{spinner_frame(now)}[/]"
        if self.status == "waiting":
            return "[$warning]?[/]"
        if self.status == "failed":
            return "[$error]✗[/]"
        if self.status == "done":
            return "[$success]✓[/]"
        return "[$secondary]◇[/]" if self.kind == "think" else "[$accent]•[/]"

    def _sync(self, now: float | None = None) -> None:
        if not self._ready:
            return
        now = time.monotonic() if now is None else now
        self.set_class(self._is_open and self.has_body, "-open")
        self.set_class(self.status == "failed", "-failed")
        chevron = "▾" if self._is_open else "▸"
        chevron = f"[$foreground-muted]{chevron}[/]" if self.has_body else " "
        if self.kind == "think":
            main = f"{chevron} {self._glyph(now)} [italic $foreground-muted]{escape(self.label)}[/]"
        else:
            detail = f"  [$foreground-muted]{escape(self.detail)}[/]" if self.detail else ""
            main = f"{chevron} {self._glyph(now)} [bold $accent]{escape(self.label)}[/]{detail}"
        if self.status == "waiting":
            meta = "[$warning]awaiting approval[/]"
        elif self.status == "running":
            meta = f"[$foreground-muted]{format_duration(now - self._t0)}[/]"
        elif self.status in ("done", "failed"):
            lines = f" · {self.line_count} lines" if self.line_count > SHORT_OUTPUT else ""
            meta = f"[$foreground-muted]{format_duration(self._elapsed)}{lines}[/]"
        else:
            meta = ""
        try:
            self.query_one(".dh-main", Static).update(main)
            self.query_one(".dh-meta", Static).update(meta)
            if self._is_open and self.has_body:
                self.query_one(".dh-body", Static).update(self._body_markup())
        except NoMatches:
            return  # torn down between the tick and now

    def _body_markup(self) -> str:
        limit = TAIL_RUNNING if self.status == "running" else TAIL_OPEN
        lines = list(self._lines)
        hidden = self._dropped
        if len(lines) > limit:
            hidden += len(lines) - limit
            lines = lines[-limit:]
        out: list[str] = []
        if hidden:
            out.append(f"[$foreground-muted italic]… {hidden} earlier lines hidden[/]")
        for line in lines:
            if self.kind == "think":
                out.append(f"[italic $foreground-muted]{escape(line)}[/]")
            elif self.diff:
                out.append(f"[{DIFF_MARKUP[diff_kind(line)]}]{escape(line) or ' '}[/]")
            else:
                out.append(f"[$foreground-muted]{escape(line) or ' '}[/]")
        return "\n".join(out)
