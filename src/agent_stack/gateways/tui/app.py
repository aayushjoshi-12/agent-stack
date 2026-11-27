"""The Textual TUI gateway.

Drives the :class:`Application`: the conversation streams user and assistant messages,
collapsible thinking and tool cards (live spinner, duration, capped shell output) and
subagent rows; a context sidebar shows session, context usage, activity, model and mode; a
footer lists the keys that matter now and ``F1`` opens the full list. It touches only
``Application``, ``gateways.base`` and ``core.events``: every run goes through
``Application.send_message`` and every cancel through ``Application.cancel_run``.

Each open conversation is a tab (:mod:`tabs`). A tab's run keeps going while another tab is
shown: every run's events are routed to the :class:`Conversation` that started it.

Layout is responsive through Textual breakpoints (ADR 012): the screen gets
``-narrow``/``-normal``/``-wide`` and ``-short``/``-tall`` classes that the stylesheet
(``styles.tcss``) reacts to. Colours live in ``theme.py``; the stylesheet only names roles.

:class:`TuiApp` is this module (layout, bindings, keyboard focus, cancel and toggles) plus one
mixin per concern: ``runs.py`` (a run's events into its tab), ``sessions.py`` (tabs and stored
conversations), ``commands.py`` (the prompt and ``/`` commands), ``timeline.py`` (rewind,
branches, references, files) and ``status.py`` (tab bar, sidebar, prompt placeholder). Widgets
live in ``rows.py``, ``chrome.py`` and ``screens.py``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, ClassVar, cast

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import Hit, Hits, Provider
from textual.containers import Horizontal, Vertical
from textual.widgets import ContentSwitcher, Footer, OptionList, Static

from agent_stack.app.application import Application, IdentityResolver
from agent_stack.gateways.tui.chrome import HelpScreen, PromptArea, Sidebar
from agent_stack.gateways.tui.commands import CommandsMixin
from agent_stack.gateways.tui.runs import RunsMixin
from agent_stack.gateways.tui.sessions import SessionsMixin
from agent_stack.gateways.tui.status import StatusMixin
from agent_stack.gateways.tui.tabs import TabBar
from agent_stack.gateways.tui.theme import DARK, DEFAULT_THEME, LIGHT, THEMES
from agent_stack.gateways.tui.timeline import TimelineMixin

__all__ = ["TuiApp", "TuiCommands"]

#: Two Escapes within this many seconds count as "esc esc".
_DOUBLE_ESC_SECONDS = 0.8


class TuiCommands(Provider):
    """App commands surfaced in the Ctrl+P command palette."""

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        app = cast("TuiApp", self.app)
        commands: list[tuple[str, Any]] = [
            ("Cancel current run", app.action_cancel_run),
            ("New tab", app.action_new_session),
            ("Close this tab", app.action_close_tab),
            ("Next tab", app.action_next_tab),
            ("Previous tab", app.action_previous_tab),
            ("Switch conversation…", app.action_switch_session),
            ("Switch approval mode (ask / auto)", app.action_toggle_approval_mode),
            ("Rewind to an earlier message…", app.action_rewind),
            ("Show conversation branches", app.action_show_branches),
            ("Copy a reference to this point", app.action_make_reference),
            ("Put this conversation's files in the folder", app.action_restore_files),
            ("Toggle sidebar", app.action_toggle_sidebar),
            ("Switch dark / light theme", app.action_toggle_theme),
            ("Keyboard shortcuts", app.action_help),
            ("Interrupt last subagent", app.action_interrupt_subagent),
            ("Focus prompt", lambda: app.query_one("#prompt", PromptArea).focus()),
            ("Quit agent-stack", app.exit),
        ]
        for name, callback in commands:
            score = matcher.match(name)
            if score > 0:
                yield Hit(score, matcher.highlight(name), callback, text=name)


class TuiApp(CommandsMixin, TimelineMixin, SessionsMixin, RunsMixin, StatusMixin):
    """The first real gateway: a Textual terminal UI over the Application."""

    TITLE = "agent-stack"
    CSS_PATH = "styles.tcss"
    #: Footer keys register their descriptions as tooltips; on short terminals the popup
    #: lands on the composer. The descriptions are already printed in the footer.
    TOOLTIP_DELAY = 3600.0
    # Textual declares these as instance attributes, so mypy rejects ClassVar here.
    HORIZONTAL_BREAKPOINTS = [  # noqa: RUF012
        (0, "-narrow"),
        (100, "-normal"),
        (140, "-wide"),
    ]
    VERTICAL_BREAKPOINTS = [(0, "-short"), (24, "-tall")]  # noqa: RUF012

    BINDINGS: ClassVar[list[Any]] = [
        Binding("ctrl+c", "cancel_or_quit", "cancel/quit", show=False, priority=True),
        Binding("escape", "escape_or_focus", "cancel", key_display="esc"),
        Binding("ctrl+n", "new_session", "new tab"),
        Binding("ctrl+pagedown", "next_tab", "next tab", show=False),
        Binding("ctrl+pageup", "previous_tab", "previous tab", show=False),
        *(Binding(f"alt+{n}", f"go_to_tab({n - 1})", f"tab {n}", show=False) for n in range(1, 10)),
        Binding("ctrl+o", "switch_session", "chats"),
        Binding("ctrl+g", "show_changes", "files"),
        Binding("ctrl+b", "toggle_sidebar", "sidebar"),
        Binding("ctrl+t", "toggle_theme", "theme", show=False),
        Binding("f1", "help", "help"),
    ]
    COMMANDS: ClassVar[set[Any]] = {*App.COMMANDS, TuiCommands}

    def __init__(
        self,
        application: Application,
        *,
        principal: str = "local-user",
        session_id: str | None = None,
        resume: bool = False,
    ) -> None:
        super().__init__()
        self.application = application
        self._principal = IdentityResolver().resolve("tui", principal)
        self._resume = resume  # open ``session_id``'s history at startup

        for theme in THEMES:
            self.register_theme(theme)
        wanted = application.config.theme
        self.theme = wanted if wanted in self.available_themes else DEFAULT_THEME

        self._last_escape = 0.0
        self._branch = ""
        self._sidebar_override: bool | None = None
        self._titles: dict[str, str] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._tab_seq = 0
        self.tab = self._new_conversation()  # the tab on screen
        self.tab.session_id = session_id
        self._tabs = [self.tab]

    # ── layout ────────────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        yield TabBar(id="tabs")
        with Horizontal(id="body"):
            with Vertical(id="conversation"):
                yield ContentSwitcher(self.tab, id="chats", initial=self.tab.id)
                with Vertical(id="composer"):
                    yield OptionList(id="completions")
                    yield PromptArea(id="prompt")
                    yield Static(id="attachments")
            yield Sidebar(id="sidebar")
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#prompt", PromptArea).focus()
        self.set_interval(0.1, self._tick)
        self._refresh_all()
        self._spawn(self._load_branch())
        if self._resume and self.tab.session_id is not None:  # a conversation to continue
            self._spawn(self._resume_session(self.tab.session_id))

    # ── the prompt keeps the keyboard ─────────────────────────────────────────
    def on_mouse_down(self) -> None:
        """A click on the conversation, a card or the sidebar hands the keyboard back to the
        prompt once the click has done its job (dialogs keep their own focus)."""
        self.call_after_refresh(self._refocus_prompt)

    def _refocus_prompt(self) -> None:
        if len(self.screen_stack) == 1 and not isinstance(self.focused, PromptArea | OptionList):
            self.query_one("#prompt", PromptArea).focus()

    def on_key(self, event: events.Key) -> None:
        """Typing while something else has focus goes into the prompt."""
        prompt = self.query_one("#prompt", PromptArea)
        if (
            len(self.screen_stack) == 1
            and self.focused is not prompt
            and event.is_printable
            and event.character
        ):
            prompt.focus()
            prompt.insert(event.character)
            event.stop()

    @property
    def session_title(self) -> str | None:
        return self.tab.title

    @property
    def resume_command(self) -> str | None:
        """How to come back to this conversation, once it has anything in it."""
        if self.tab.session_id is None or (self.tab.title is None and not self.tab.tokens):
            return None
        return f"agent-stack resume {self.tab.session_id}"

    @property
    def queued_count(self) -> int:
        """Messages in this tab waiting for the run to read them (up arrow takes one back)."""
        return len(self.tab.queued)

    def on_unmount(self) -> None:
        for task in list(self._tasks):
            task.cancel()

    async def _load_branch(self) -> None:
        """Best-effort ``git branch --show-current`` for the sidebar."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                str(self.application.config.workspace),
                "branch",
                "--show-current",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=2)
            branch = out.decode().strip()
        except Exception:
            branch = ""
        if branch and self.is_running and len(self.query("#sidebar")):
            self._branch = branch
            self._refresh_sidebar()

    def _busy(self) -> bool:
        """True (after telling the user) while this tab's run is active."""
        if self.tab.running:
            self.notify("A run is in progress. Press esc to cancel it first.", severity="warning")
        return self.tab.running

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ── actions ──────────────────────────────────────────────────────────────
    async def action_cancel_run(self) -> None:
        tab = self.tab
        if tab.run_id is not None and tab.running and not tab.cancel_sent:
            tab.cancel_sent = True
            self._refresh_all()
            await self.application.cancel_run(tab.run_id)

    async def action_cancel_or_quit(self) -> None:
        if self.tab.running and not self.tab.cancel_sent:
            await self.action_cancel_run()
        else:
            self.exit()

    async def action_escape_or_focus(self) -> None:
        """Esc returns control: cancel a live run, else focus the prompt; esc esc rewinds.

        A second Esc within a moment clears what is typed, or, with an empty prompt, opens the
        rewind picker (the Claude Code habit).
        """
        if self.tab.running and not self.tab.cancel_sent:
            await self.action_cancel_run()
            return
        prompt = self.query_one("#prompt", PromptArea)
        now = time.monotonic()
        double = now - self._last_escape < _DOUBLE_ESC_SECONDS
        self._last_escape = 0.0 if double else now
        if not double:
            prompt.focus()
        elif prompt.text.strip():
            prompt.clear()
        else:
            await self.action_rewind()

    def action_scroll_chat(self, key: str) -> None:
        if key == "pageup":
            self.tab.scroll_page_up()
        else:
            self.tab.scroll_page_down()

    def action_toggle_sidebar(self) -> None:
        """Flip the sidebar against the width default; a second press returns to automatic.

        The override never sticks: otherwise show → hide → show would pin the sidebar open
        and break the responsive layout on the next resize.
        """
        if self._sidebar_override is None:
            visible = self.query_one("#sidebar").styles.display != "none"
            self._sidebar_override = not visible
        else:
            self._sidebar_override = None
        self.screen.set_class(self._sidebar_override is True, "-show-sidebar")
        self.screen.set_class(self._sidebar_override is False, "-hide-sidebar")

    def action_toggle_theme(self) -> None:
        self.theme = LIGHT.name if self.theme == DARK.name else DARK.name

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    async def action_interrupt_subagent(self) -> None:
        if self.tab.last_child_id is not None:
            await self.application.cancel_run(self.tab.last_child_id)
