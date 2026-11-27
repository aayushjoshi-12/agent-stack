"""The chrome around the conversation: the prompt, the context sidebar, the welcome panel
and the help overlay.

They hold no runtime state beyond their own presentation; the app fills the sidebar from a
:class:`SidebarState` it rebuilds on change (``status.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

from textual import events
from textual.binding import Binding
from textual.containers import Vertical
from textual.markup import escape
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static, TextArea

__all__ = ["HelpScreen", "PromptArea", "Sidebar", "SidebarState", "Welcome"]


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
        elif key == "up" and not self.text and int(getattr(self.app, "queued_count", 0)) > 0:
            event.stop()
            event.prevent_default()
            await self.app.run_action("edit_queued")
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


# ── sidebar, welcome, help ────────────────────────────────────────────────────
@dataclass
class SidebarState:
    """Everything the sidebar shows; the app rebuilds this on change."""

    title: str = "New conversation"
    session: str = ""
    tokens: int = 0
    #: ``tokens`` is a rough count of the text, not yet a size the model reported.
    estimated: bool = True
    window: int = 200_000
    tools_run: int = 0
    tools_running: int = 0
    subagents: list[tuple[str, str]] = field(default_factory=list)
    workspace: str = ""
    branch: str = ""
    state: str = "idle"
    cost: str = ""
    mode: str = "ask"
    model: str = ""


class Sidebar(Vertical):
    """The context panel: session, context, activity, subagents; model, mode and workspace below."""

    BAR = 26

    def compose(self):  # type: ignore[no-untyped-def]
        yield Static("", id="sb-session", classes="sb-section")
        yield Static("", id="sb-context", classes="sb-section")
        yield Static("", id="sb-activity", classes="sb-section")
        yield Static("", id="sb-subagents", classes="sb-section")
        yield Static("", id="sb-spacer")
        yield Static("", id="sb-model", classes="sb-section")
        yield Static("", id="sb-mode", classes="sb-section")
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
        approx = "~" if s.estimated else ""
        self.query_one("#sb-context", Static).update(
            f"[$foreground-muted]CONTEXT[/]\n{bar}\n"
            f"{approx}{s.tokens / 1000:.1f}K / {s.window // 1000}K [$foreground-muted]· {pct}%[/]"
        )
        running = f"  [$accent]{s.tools_running} running[/]" if s.tools_running else ""
        self.query_one("#sb-activity", Static).update(
            f"[$foreground-muted]ACTIVITY[/]\n{s.state}\n"
            f"{s.tools_run} tool call{'' if s.tools_run == 1 else 's'}{running}"
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
        self.query_one("#sb-model", Static).update(
            f"[$foreground-muted]MODEL[/]\n{escape(s.model)}"
        )
        mode = "[bold $warning]auto[/]" if s.mode == "auto" else "ask"
        self.query_one("#sb-mode", Static).update(f"[$foreground-muted]APPROVALS[/]\n{mode}")
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
            ("up", "on an empty prompt during a run: take back the last queued message"),
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
            ("ctrl+p", "command palette (themes, tabs, …)"),
            ("ctrl+n", "new tab (other tabs keep running)"),
            ("alt+1..9", "go to tab 1 to 9 (or click it)"),
            ("ctrl+pgup / pgdn", "previous / next tab"),
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
