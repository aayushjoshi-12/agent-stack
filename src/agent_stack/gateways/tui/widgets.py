"""Widgets for the TUI gateway.

Small, single-purpose pieces the app composes: message rows, the collapsible
:class:`Disclosure` card used for tool calls and thinking, the multi-line
:class:`PromptArea`, the header, the context sidebar and the help overlay. They hold no
runtime state beyond their own presentation; :mod:`agent_stack.gateways.tui.app` routes
events into them. Colours come from theme variables (``[$accent]`` markup and the
stylesheet), never literals, so every theme restyles them.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from textual import events
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.markup import escape
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Markdown, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from agent_stack.app.application import FileDiff
from agent_stack.gateways.base import SessionSummary, format_when
from agent_stack.gateways.tui.render import diff_kind

if TYPE_CHECKING:
    from agent_stack.app.application import LineTurn

__all__ = [
    "AgentMessage",
    "ApprovalScreen",
    "ChangesScreen",
    "Disclosure",
    "HeaderBar",
    "HelpScreen",
    "Notice",
    "PromptArea",
    "SessionPicker",
    "Sidebar",
    "SidebarState",
    "SubagentRow",
    "UserMessage",
    "Welcome",
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

_DIFF_MARKUP = {
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

    def __init__(self, text: str) -> None:
        super().__init__(escape(text), classes="user-message")


class AgentMessage(Markdown):
    """An assistant reply, rendered as Markdown."""

    def __init__(self, content: str) -> None:
        super().__init__(content, classes="agent-message")


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
                out.append(f"[{_DIFF_MARKUP[diff_kind(line)]}]{escape(line) or ' '}[/]")
            else:
                out.append(f"[$foreground-muted]{escape(line) or ' '}[/]")
        return "\n".join(out)


# ── composer ─────────────────────────────────────────────────────────────────
class PromptArea(TextArea):
    """Multi-line input: Enter sends, Shift+Enter / Ctrl+J inserts a newline.

    Up/Down walk the sent-message history when the cursor is on the first/last line;
    Esc and Page Up/Down are forwarded to the app (cancel / scroll the conversation).
    """

    class Submitted(Message):
        """Posted when the user presses Enter on non-empty text."""

        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def __init__(self, **kwargs: object) -> None:
        super().__init__(
            soft_wrap=True,
            show_line_numbers=False,
            highlight_cursor_line=False,
            tab_behavior="focus",
            **kwargs,  # type: ignore[arg-type]
        )
        self._history: list[str] = []
        self._cursor = 0  # == len(history) when editing a fresh message
        self._draft = ""

    def _completions(self) -> OptionList | None:
        """The ``/`` completion list, when it is showing."""
        found = self.app.query("#completions")
        popup = found.first(OptionList) if found else None
        return popup if popup is not None and popup.display else None

    def accept_completion(self) -> bool:
        """Replace what is typed with the highlighted ``/command``; False when none is showing."""
        popup = self._completions()
        if popup is None or popup.highlighted is None:
            return False
        name = popup.get_option_at_index(popup.highlighted).id
        self.load_text(f"/{name} ")
        self.move_cursor(self.document.end)
        popup.display = False
        return True

    async def _on_key(self, event: events.Key) -> None:
        key = event.key
        popup = self._completions()
        if popup is not None:
            if key in ("down", "up"):
                event.stop()
                event.prevent_default()
                popup.action_cursor_down() if key == "down" else popup.action_cursor_up()
                return
            if key == "tab":
                event.stop()
                event.prevent_default()
                self.accept_completion()
                return
            if key == "escape":
                event.stop()
                event.prevent_default()
                popup.display = False
                return
            if key == "enter" and popup.highlighted is not None:
                chosen = popup.get_option_at_index(popup.highlighted).id
                if self.text.strip() != f"/{chosen}":  # still typing it: finish it first
                    event.stop()
                    event.prevent_default()
                    self.accept_completion()
                    return
        if key == "shift+tab":  # in the prompt this switches approval mode (tab still moves focus)
            event.stop()
            event.prevent_default()
            await self.app.run_action("toggle_approval_mode")
        elif key == "enter":
            event.stop()
            event.prevent_default()
            self._submit()
        elif key in ("shift+enter", "ctrl+j", "alt+enter"):
            event.stop()
            event.prevent_default()
            self.insert("\n")
        elif key == "escape":
            event.stop()
            event.prevent_default()
            await self.app.run_action("escape_or_focus")
        elif key in ("pageup", "pagedown"):
            event.stop()
            event.prevent_default()
            await self.app.run_action(f"scroll_chat('{key}')")
        elif key == "up" and self.cursor_location[0] == 0 and self._history:
            event.stop()
            event.prevent_default()
            self._recall(-1)
        elif key == "down" and self._at_last_line() and self._cursor < len(self._history):
            event.stop()
            event.prevent_default()
            self._recall(1)
        else:
            await super()._on_key(event)

    def _at_last_line(self) -> bool:
        return self.cursor_location[0] >= self.document.line_count - 1

    def _submit(self) -> None:
        text = self.text.strip()
        if not text:
            return
        if not self._history or self._history[-1] != text:
            self._history.append(text)
        self._cursor = len(self._history)
        self.post_message(self.Submitted(text))

    def clear_text(self) -> None:
        self.load_text("")

    def _recall(self, delta: int) -> None:
        if self._cursor == len(self._history):
            self._draft = self.text
        self._cursor = max(0, min(len(self._history), self._cursor + delta))
        text = self._draft if self._cursor == len(self._history) else self._history[self._cursor]
        self.load_text(text)
        self.move_cursor(self.document.end)


# ── chrome: header, sidebar, welcome, help ───────────────────────────────────
class HeaderBar(Horizontal):
    """Brand + session title on the left; run state and model on the right."""

    def compose(self):  # type: ignore[no-untyped-def]
        yield Static("◆ agent-stack", id="brand")
        yield Static("", id="title")
        yield Static("", id="run-state")
        yield Static("", id="model")

    def _set(self, selector: str, markup: str) -> None:
        # The repaint tick can fire while the app is tearing down and the children are
        # already gone; a missing child is then simply nothing to update.
        for widget in self.query(selector):
            if isinstance(widget, Static):
                widget.update(markup)

    def set_title(self, title: str) -> None:
        self._set("#title", escape(title))

    def set_model(self, model: str) -> None:
        self._set("#model", escape(model))

    def set_state(self, markup: str) -> None:
        self._set("#run-state", markup)


@dataclass
class SidebarState:
    """Everything the sidebar shows; the app rebuilds this on change."""

    title: str = "New conversation"
    session: str = ""
    tokens: int = 0
    window: int = 200_000
    tools_run: int = 0
    tools_running: int = 0
    subagents: list[tuple[str, str]] = field(default_factory=list)
    workspace: str = ""
    branch: str = ""
    state: str = "idle"
    cost: str = ""
    mode: str = "ask"


class Sidebar(Vertical):
    """The context panel: session, context usage, activity, subagents, workspace."""

    BAR = 26

    def compose(self):  # type: ignore[no-untyped-def]
        yield Static("", id="sb-session", classes="sb-section")
        yield Static("", id="sb-context", classes="sb-section")
        yield Static("", id="sb-activity", classes="sb-section")
        yield Static("", id="sb-subagents", classes="sb-section")
        yield Static("", id="sb-spacer")
        yield Static("", id="sb-workspace", classes="sb-section")

    def render_state(self, s: SidebarState) -> None:
        pct = min(100, round(s.tokens / s.window * 100)) if s.window else 0
        filled = round(self.BAR * pct / 100)
        tone = "$error" if pct >= 90 else "$warning" if pct >= 70 else "$accent"
        bar = f"[{tone}]{'━' * filled}[/][$foreground-muted]{'─' * (self.BAR - filled)}[/]"
        self.query_one("#sb-session", Static).update(
            f"[$foreground-muted]SESSION[/]\n[bold]{escape(s.title)}[/]\n"
            f"[$foreground-muted]{escape(s.session)}[/]"
        )
        self.query_one("#sb-context", Static).update(
            f"[$foreground-muted]CONTEXT[/]\n{bar}\n"
            f"{s.tokens / 1000:.1f}K / {s.window // 1000}K [$foreground-muted]· {pct}%[/]"
        )
        running = f"  [$accent]{s.tools_running} running[/]" if s.tools_running else ""
        self.query_one("#sb-activity", Static).update(
            f"[$foreground-muted]ACTIVITY[/]\n{s.tools_run} tool call{'' if s.tools_run == 1 else 's'}{running}\nstatus: {s.state}"
            + ("\napprovals: [bold $warning]auto[/]" if s.mode == "auto" else "\napprovals: ask")
            + (f"\n[$foreground-muted]spent[/] {escape(s.cost)}" if s.cost else "")
        )
        if s.subagents:
            rows = "\n".join(
                f"{'[$success]✓[/]' if st == 'completed' else '[$accent]●[/]' if st == 'running' else '[$error]✗[/]'}"
                f" {escape(name)} [$foreground-muted]{escape(st)}[/]"
                for name, st in s.subagents[-4:]
            )
            self.query_one("#sb-subagents", Static).update(
                f"[$foreground-muted]SUBAGENTS[/]\n{rows}"
            )
        else:
            self.query_one("#sb-subagents", Static).update("")
        where = f"{escape(s.workspace)}" + (
            f"\n[$foreground-muted]⎇ {escape(s.branch)}[/]" if s.branch else ""
        )
        self.query_one("#sb-workspace", Static).update(f"[$foreground-muted]WORKSPACE[/]\n{where}")


class Welcome(Static):
    """The empty-conversation hint: what to type and which keys matter."""

    def __init__(self) -> None:
        text = (
            "[bold $primary]◆ agent-stack[/]\n"
            "[$foreground-muted]Ask for something. Tools, subagents and thinking appear as you go.[/]\n\n"
            "  [bold $accent]enter[/] send    [bold $accent]ctrl+j[/] new line    "
            "[bold $accent]esc[/] cancel\n"
            "  [bold $accent]ctrl+p[/] commands    [bold $accent]f1[/] all shortcuts\n"
            "  [bold $accent]@name task[/] delegate to a subagent"
        )
        super().__init__(text, id="welcome")


HELP = (
    (
        "Conversation",
        (
            ("enter", "send message"),
            ("ctrl+j / shift+enter", "new line (shift+enter needs a modern terminal)"),
            ("up / down", "message history (on first / last line)"),
            ("pageup / pagedown", "scroll the conversation"),
            ("esc", "cancel the run, or return to the prompt"),
            ("esc esc", "rewind: go back to an earlier message (clears typed text first)"),
            ("/", "commands and skills: type / for a list, tab completes"),
            (
                "shift+tab",
                "in the prompt: switch approval mode, ask you or auto (a classifier decides)",
            ),
            ("tab / shift+tab", "move between prompt and tool cards"),
            ("enter / click", "expand or collapse a focused card"),
        ),
    ),
    (
        "App",
        (
            ("ctrl+p", "command palette (themes, new conversation, …)"),
            ("ctrl+n", "new conversation (the current one is kept)"),
            ("ctrl+o", "switch to an earlier conversation"),
            ("ctrl+g", "files the agent changed, with their diffs"),
            ("ctrl+b", "show or hide the context sidebar"),
            ("ctrl+t", "switch dark / light theme"),
            ("ctrl+c", "cancel the run; press again to quit"),
            ("ctrl+q", "quit"),
            ("f1", "this help"),
        ),
    ),
)


class HelpScreen(ModalScreen[None]):
    """The full shortcut list, opened with F1 and closed with Esc / F1 / q."""

    BINDINGS: ClassVar[list[Any]] = [
        Binding("escape,f1,q", "dismiss_help", "close"),
    ]

    def compose(self):  # type: ignore[no-untyped-def]
        with Vertical(id="help-card"):
            yield Static("[bold $primary]Keyboard shortcuts[/]", id="help-title")
            for group, rows in HELP:
                lines = [f"[bold $foreground-muted]{group.upper()}[/]"]
                lines += [f"  [bold $accent]{key:<22}[/] {desc}" for key, desc in rows]
                yield Static("\n".join(lines), classes="help-group")
            yield Static("[$foreground-muted]esc to close[/]", id="help-foot")

    def action_dismiss_help(self) -> None:
        self.dismiss(None)


class ChangesScreen(ModalScreen[None]):
    """The files the agent created or edited: pick one to read its diff.

    Shows the *net* change per file (first state to last), as the web UI does. Only
    ``write_file``/``edit_file`` are tracked, which the footer says.
    """

    BINDINGS: ClassVar[list[Any]] = [Binding("escape,q,ctrl+g", "close", "close")]
    FOOT = (
        "[$foreground-muted]↑↓ choose · esc close · only files the agent wrote or edited "
        "with its file tools are listed, not files changed by shell commands[/]"
    )

    def __init__(self, files: list[FileDiff]) -> None:
        super().__init__()
        self._files = files

    def compose(self):  # type: ignore[no-untyped-def]
        with Vertical(id="changes-card"):
            count = len(self._files)
            yield Static(
                f"[bold $primary]Files changed[/]  [$foreground-muted]{count} "
                f"file{'' if count == 1 else 's'}[/]",
                id="changes-title",
            )
            if not self._files:
                yield Static("[$foreground-muted]The agent has not changed any files yet.[/]")
            else:
                yield OptionList(*(self._option(f) for f in self._files), id="changes-list")
                with VerticalScroll(id="changes-scroll"):
                    yield Static("", id="changes-diff")
            yield Static(self.FOOT, id="changes-foot")

    @staticmethod
    def _option(file: FileDiff) -> Option:
        kind = "[$success]new[/]" if file.kind == "created" else "[$accent]edit[/]"
        edits = f" x{file.edits}" if file.edits > 1 else ""
        stats = f"[$success]+{file.added}[/] [$error]-{file.removed}[/]"
        return Option(
            f"{kind} {escape(file.path)}  {stats}[$foreground-muted]{edits}[/]", id=file.path
        )

    def on_mount(self) -> None:
        if self._files:
            listing = self.query_one(OptionList)
            listing.focus()
            listing.highlighted = 0
            self._show(self._files[0])

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        for file in self._files:
            if file.path == event.option.id:
                self._show(file)
                return

    def _show(self, file: FileDiff) -> None:
        if not file.diff:
            body = "[$foreground-muted]No net difference: the edits cancelled each other out.[/]"
        else:
            body = "\n".join(
                f"[{_DIFF_MARKUP[diff_kind(line)]}]{escape(line) or ' '}[/]"
                for line in file.diff.splitlines()
            )
            if file.diff_truncated:
                body += "\n[$foreground-muted italic]… diff cut at 4000 lines[/]"
        self.query_one("#changes-diff", Static).update(body)
        self.query_one("#changes-scroll", VerticalScroll).scroll_home(animate=False)

    def action_close(self) -> None:
        self.dismiss(None)


class SessionPicker(ModalScreen[str | None]):
    """Pick an earlier conversation: Enter opens it, ``d`` (twice) deletes it, Esc closes.

    ``delete`` is supplied by the app, so the picker never touches storage itself.
    """

    BINDINGS: ClassVar[list[Any]] = [
        Binding("escape,q", "close", "close"),
        Binding("d,delete", "delete", "delete"),
    ]
    FOOT = "[$foreground-muted]enter open · d delete · esc close[/]"

    def __init__(
        self,
        sessions: list[SessionSummary],
        current: str | None,
        delete: Callable[[str], Awaitable[None]],
    ) -> None:
        super().__init__()
        self._sessions = sessions
        self._current = current
        self._delete = delete
        self._pending: str | None = None  # the id awaiting a second `d`

    def compose(self):  # type: ignore[no-untyped-def]
        with Vertical(id="picker-card"):
            yield Static("[bold $primary]Conversations[/]", id="picker-title")
            yield OptionList(*(self._option(s) for s in self._sessions), id="picker-list")
            yield Static(self.FOOT if self._sessions else self._EMPTY, id="picker-foot")

    _EMPTY = "[$foreground-muted]No conversations yet. esc to close[/]"

    def _option(self, session: SessionSummary) -> Option:
        """Two lines and a gap: the title on one line, its date and kind beneath, muted."""
        here = session.id == self._current
        mark = "[$accent]●[/] " if here else "  "
        tag = "  [$accent](open now)[/]" if here else ""
        title = " ".join(session.title.split())
        shown = title if len(title) <= 60 else title[:59] + "…"
        facts = [format_when(session.updated_at or session.created_at)]
        if session.branch_of:
            facts.append("a branch")
        meta = "  ·  ".join(f for f in facts if f)
        lines = [
            f"{mark}[bold]{escape(shown)}[/]{tag}",
            f"  [$foreground-muted]{escape(meta)}[/]",
            "",
        ]
        return Option("\n".join(lines), id=session.id)

    def on_mount(self) -> None:
        listing = self.query_one(OptionList)
        listing.focus()
        for index, session in enumerate(self._sessions):
            if session.id == self._current:
                listing.highlighted = index

    def _foot(self, markup: str) -> None:
        self.query_one("#picker-foot", Static).update(markup)

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if self._pending is not None and event.option.id != self._pending:
            self._pending = None
            self._foot(self.FOOT)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    async def action_delete(self) -> None:
        listing = self.query_one(OptionList)
        if listing.highlighted is None or listing.option_count == 0:
            return
        session_id = listing.get_option_at_index(listing.highlighted).id
        if session_id is None:
            return
        if self._pending != session_id:  # first press asks, second press deletes
            self._pending = session_id
            self._foot("[$warning]Press d again to delete this conversation for good[/]")
            return
        self._pending = None
        try:
            await self._delete(session_id)
        except Exception as exc:  # a failed delete must not take the picker (or app) down
            self.notify(f"Could not delete: {exc}", severity="error")
            return
        listing.remove_option(session_id)
        self._foot(self.FOOT if listing.option_count else self._EMPTY)

    def action_close(self) -> None:
        self.dismiss(None)


class BranchesScreen(ModalScreen[None]):
    """The branch tree of the open conversation as text; Esc closes."""

    BINDINGS: ClassVar[list[Any]] = [Binding("escape,q", "close", "close")]

    def __init__(self, text: str) -> None:
        super().__init__()
        self._text = text

    def compose(self):  # type: ignore[no-untyped-def]
        with Vertical(id="picker-card", classes="wide"):
            yield Static(
                "[bold $primary]Branches[/]  [$foreground-muted]* is this one[/]", id="picker-title"
            )
            with VerticalScroll(id="changes-scroll"):
                yield Static(escape(self._text))
            yield Static("[$foreground-muted]esc close[/]", id="picker-foot")

    def action_close(self) -> None:
        self.dismiss(None)


#: What a rewind restores, in the order the picker offers them.
REWIND_CHOICES = (
    ("both", "Restore code and conversation"),
    ("conversation", "Restore conversation (keep the code as it is now)"),
    ("code", "Restore code (keep the conversation)"),
    ("cancel", "Never mind"),
)


class RewindScreen(ModalScreen["tuple[LineTurn, str] | None"]):
    """Go back to an earlier message, like Claude Code's double-Esc.

    Step one picks the message (newest highlighted, since that is usually the one you want),
    step two what to restore. Nothing is deleted: the conversation you leave stays in the chats
    list, so a rewind can always be undone by opening it again.
    """

    BINDINGS: ClassVar[list[Any]] = [Binding("escape,q", "back", "back")]
    FOOT_TURNS = "[$foreground-muted]enter choose · esc close[/]"
    FOOT_MODE = "[$foreground-muted]enter restore · esc back[/]"

    def __init__(self, turns: list[LineTurn]) -> None:
        super().__init__()
        self._turns = turns
        self._picked: LineTurn | None = None

    def compose(self):  # type: ignore[no-untyped-def]
        with Vertical(id="picker-card"):
            yield Static(
                "[bold $primary]Rewind[/]  [$foreground-muted]go back to…[/]", id="picker-title"
            )
            yield OptionList(*self._turn_options(), id="picker-list")
            yield Static(self.FOOT_TURNS, id="picker-foot")

    def _turn_options(self) -> list[Option]:
        rows = []
        for number, item in enumerate(self._turns):
            files = item.turn.files_changed
            tail = (
                f"  [$foreground-muted]{files} file{'s' if files != 1 else ''}[/]" if files else ""
            )
            text = escape(" ".join(item.turn.user_text.split())[:60] or "(no text)")
            when = format_when(item.turn.timestamp.isoformat())
            rows.append(
                Option(
                    f"[bold]{text}[/]\n  [$foreground-muted]{escape(when)}[/]{tail}\n",
                    id=str(number),
                )
            )
        return rows

    def on_mount(self) -> None:
        listing = self.query_one(OptionList)
        listing.focus()
        listing.highlighted = max(0, len(self._turns) - 1)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        listing = self.query_one(OptionList)
        if self._picked is None:
            self._picked = self._turns[int(event.option.id or 0)]
            listing.clear_options()
            listing.add_options([Option(label, id=key) for key, label in REWIND_CHOICES])
            listing.highlighted = 0
            title = escape(" ".join(self._picked.turn.user_text.split())[:50])
            self.query_one("#picker-title", Static).update(
                f"[bold $primary]Rewind to before[/] {title}"
            )
            self.query_one("#picker-foot", Static).update(self.FOOT_MODE)
            return
        if event.option.id == "cancel":
            self.dismiss(None)
        else:
            self.dismiss((self._picked, str(event.option.id)))

    def action_back(self) -> None:
        if self._picked is None:
            self.dismiss(None)
            return
        self._picked = None
        listing = self.query_one(OptionList)
        listing.clear_options()
        listing.add_options(self._turn_options())
        listing.highlighted = max(0, len(self._turns) - 1)
        self.query_one("#picker-title", Static).update(
            "[bold $primary]Rewind[/]  [$foreground-muted]go back to…[/]"
        )
        self.query_one("#picker-foot", Static).update(self.FOOT_TURNS)


class ApprovalScreen(ModalScreen[bool]):
    """Ask whether a gated tool call may run: ``y`` approves, ``n`` / Esc rejects.

    There is deliberately no Enter shortcut, so a stray keypress cannot approve a command.
    """

    BINDINGS: ClassVar[list[Any]] = [
        Binding("y", "answer(True)", "approve"),
        Binding("n,escape", "answer(False)", "reject"),
    ]

    def __init__(self, tool: str, detail: str, note: str = "") -> None:
        super().__init__()
        self._tool = tool
        self._detail = detail
        self._note = note  # why auto mode left this one to you

    def compose(self):  # type: ignore[no-untyped-def]
        with Vertical(id="approval-card"):
            yield Static("[bold $warning]Approval needed[/]", id="approval-title")
            yield Static(
                f"The agent wants to run [bold $accent]{escape(self._tool)}[/]:",
                id="approval-what",
            )
            yield Static(escape(self._detail) or "(no arguments)", id="approval-detail")
            if self._note:
                yield Static(
                    f"[$foreground-muted]Auto mode left this to you:[/] {escape(self._note)}",
                    id="approval-note",
                )
            yield Static(
                "[bold $success]y[/] approve    [bold $error]n[/] / esc reject",
                id="approval-foot",
            )

    def action_answer(self, approved: bool) -> None:
        self.dismiss(approved)
