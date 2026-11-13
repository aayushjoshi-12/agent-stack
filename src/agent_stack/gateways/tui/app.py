"""The Textual TUI gateway.

Drives the :class:`Application`: the conversation streams user and assistant messages,
collapsible thinking and tool cards (live spinner, duration, capped shell output) and
subagent rows; a context sidebar shows session, context usage and activity; a footer
lists the keys that matter now and ``F1`` opens the full list. It touches only
``Application``, ``gateways.base`` and ``core.events``: every run goes through
``Application.send_message`` and every cancel through ``Application.cancel_run``.

Layout is responsive through Textual breakpoints (ADR 012): the screen gets
``-narrow``/``-normal``/``-wide`` and ``-short``/``-tall`` classes that the stylesheet
(``styles.tcss``) reacts to. Colours live in ``theme.py``; the stylesheet only names roles.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, ClassVar, cast

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import Hit, Hits, Provider
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Static

from agent_stack.app.application import Application, IdentityResolver, SessionResolver
from agent_stack.core.events import AgentEvent, EventType
from agent_stack.gateways.base import MessageEnvelope
from agent_stack.gateways.tui.render import tool_label
from agent_stack.gateways.tui.theme import DARK, DEFAULT_THEME, LIGHT, THEMES
from agent_stack.gateways.tui.widgets import (
    AgentMessage,
    Disclosure,
    HeaderBar,
    HelpScreen,
    Notice,
    PromptArea,
    Sidebar,
    SidebarState,
    SubagentRow,
    UserMessage,
    Welcome,
    format_duration,
)

__all__ = ["TuiApp", "TuiCommands"]

_QUIT_WORDS = frozenset({"exit", "quit"})
#: Rough context window used for the usage meter (the estimate is ~4 chars per token).
CONTEXT_WINDOW = 200_000
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class TuiCommands(Provider):
    """App commands surfaced in the Ctrl+P command palette."""

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        app = cast("TuiApp", self.app)
        commands: list[tuple[str, Any]] = [
            ("Cancel current run", app.action_cancel_run),
            ("New conversation", app.action_clear_chat),
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


class TuiApp(App[None]):
    """The first real gateway: a Textual terminal UI over the Application."""

    TITLE = "agent-stack"
    CSS_PATH = "styles.tcss"
    # Textual declares these as instance attributes, so mypy rejects ClassVar here.
    HORIZONTAL_BREAKPOINTS = [  # noqa: RUF012
        (0, "-narrow"),
        (100, "-normal"),
        (140, "-wide"),
    ]
    VERTICAL_BREAKPOINTS = [(0, "-short"), (24, "-tall")]  # noqa: RUF012

    BINDINGS: ClassVar[list[Any]] = [
        Binding("ctrl+c", "cancel_or_quit", "cancel/quit", show=False, priority=True),
        Binding("ctrl+q", "quit", "quit", show=False),
        Binding("escape", "escape_or_focus", "cancel", key_display="esc"),
        Binding("ctrl+n", "clear_chat", "new"),
        Binding("ctrl+b", "toggle_sidebar", "sidebar"),
        Binding("ctrl+t", "toggle_theme", "theme", show=False),
        Binding("f1", "help", "help"),
    ]
    COMMANDS: ClassVar[set[Any]] = {*App.COMMANDS, TuiCommands}
    COMMAND_PALETTE_BINDING = "ctrl+p"

    def __init__(
        self,
        application: Application,
        *,
        principal: str = "local-user",
        session_id: str | None = None,
    ) -> None:
        super().__init__()
        self.application = application
        self._principal = IdentityResolver().resolve("tui", principal)
        self._session_id = session_id

        for theme in THEMES:
            self.register_theme(theme)
        wanted = application.config.theme
        self.theme = wanted if wanted in self.available_themes else DEFAULT_THEME

        self._active_run_id: str | None = None
        self._run_active = False
        self._cancel_sent = False
        self._run_started = 0.0
        self._last_outcome = ""  # "" | "done" | "failed" | "cancelled"
        self._last_elapsed = 0.0
        self._task_title: str | None = None
        self._tokens = 0
        self._branch = ""
        self._sidebar_override: bool | None = None
        self._last_child_id: str | None = None

        self.cards: dict[str, Disclosure] = {}
        self.subagent_rows: dict[str, SubagentRow] = {}
        self._subagent_state: dict[str, tuple[str, str]] = {}
        self._tools_run = 0
        self._tasks: set[asyncio.Task[None]] = set()

    # ── layout ────────────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        yield HeaderBar()
        with Horizontal(id="body"):
            with Vertical(id="conversation"):
                yield VerticalScroll(Welcome(), id="chat")
                with Vertical(id="composer"):
                    yield PromptArea(id="prompt")
                yield Static("", id="hints")
            yield Sidebar(id="sidebar")
        yield Footer()

    async def on_mount(self) -> None:
        if self._session_id is None:
            resolver = SessionResolver(self.application.session_store, self.application.agent.name)
            self._session_id = await resolver.resolve(self._principal)

        self.query_one(HeaderBar).set_model(self.application.config.model)
        self.query_one("#prompt", PromptArea).focus()
        self.set_interval(0.1, self._tick)
        self._refresh_all()
        self._spawn(self._load_branch())

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

    # ── input ─────────────────────────────────────────────────────────────────
    async def on_prompt_area_submitted(self, event: PromptArea.Submitted) -> None:
        text = event.text
        if text in _QUIT_WORDS:
            self.exit()
            return
        if self._run_active:
            self.notify("A run is in progress. Press esc to cancel it first.", severity="warning")
            return
        self.query_one("#prompt", PromptArea).clear_text()
        await self._submit_prompt(text)

    async def _submit_prompt(self, text: str) -> None:
        self._remove_welcome()
        if self._task_title is None:
            self._task_title = text
        self._bump_tokens(text)
        self._chat().mount(UserMessage(text))
        delegated = _parse_delegation(text)
        if delegated is not None:
            # Honest delegation: the app can only start a run on the wired agent, so the
            # named subagent is reached by asking the model to delegate to it (P20).
            name, task = delegated
            text = f"Delegate this task to the `{name}` subagent: {task}"
        await self._start_run(text)

    async def _start_run(self, text: str) -> None:
        envelope = MessageEnvelope(
            principal=self._principal, session=self._session_id or "", text=text
        )
        handle = await self.application.send_message(envelope)
        self._active_run_id = handle.run_id
        self._run_active = True
        self._cancel_sent = False
        self._run_started = time.monotonic()
        self._refresh_all()
        self._spawn(self._consume(handle))

    async def _consume(self, handle: Any) -> None:
        try:
            async for event in handle.events():
                self.handle_event(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a broken run must not tear down the UI
            self.notify(str(exc), severity="error")
            self._finish_run("failed")

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ── event routing ─────────────────────────────────────────────────────────
    def handle_event(self, event: AgentEvent) -> None:
        """Route one runtime event to the conversation, header and sidebar."""
        if len(self.query("#chat")) == 0:
            # The screen has been torn down (quit or test teardown) while a stream
            # was still live; drop the event rather than touching dead widgets.
            return
        if event.type is EventType.SUBAGENT_STARTED:
            self._on_subagent_started(event)
        elif event.type is EventType.SUBAGENT_COMPLETED:
            self._on_subagent_completed(event)
        elif event.parent_run_id is not None and event.run_id != self._active_run_id:
            pass  # child-run detail stays out of the parent conversation
        else:
            self._on_main_event(event)

    def _on_main_event(self, event: AgentEvent) -> None:
        etype = event.type
        if etype is EventType.RUN_STARTED:
            self._active_run_id = event.run_id
            if not self._run_active:
                self._run_active = True
                self._run_started = time.monotonic()
        elif etype is EventType.MODEL_REASONING:
            self._on_reasoning(event)
        elif etype is EventType.ASSISTANT_MESSAGE:
            self._on_assistant(event)
        elif etype is EventType.TOOL_STARTED:
            self._on_tool_started(event)
        elif etype is EventType.TOOL_OUTPUT:
            card = self.cards.get(str(event.data.get("tool_call_id")))
            if card is not None:
                card.append(str(event.data.get("chunk", "")))
        elif etype is EventType.TOOL_COMPLETED:
            card = self.cards.get(str(event.data.get("tool_call_id")))
            if card is not None:
                card.finish("done")
        elif etype is EventType.RUN_FAILED:
            self._on_failed(event)
        elif etype is EventType.RUN_COMPLETED:
            self._finish_run("done")  # a successful run adds no row to the conversation
        self._refresh_all()

    def _on_reasoning(self, event: AgentEvent) -> None:
        text = str(event.data.get("text", ""))
        if not text.strip():
            return
        self._bump_tokens(text)
        self._remove_welcome()
        self._chat().mount(Disclosure("think", "thinking", body=text))

    def _on_assistant(self, event: AgentEvent) -> None:
        content = str(event.data.get("content", ""))
        if not content.strip():
            return
        self._bump_tokens(content)
        self._remove_welcome()
        self._chat().mount(AgentMessage(content))

    def _on_tool_started(self, event: AgentEvent) -> None:
        name, detail = tool_label(event)
        card = Disclosure("tool", name, detail, running=True, opened=name == "run_command")
        self._remove_welcome()
        self._chat().mount(card)
        self._tools_run += 1
        call_id = str(event.data.get("tool_call_id", ""))
        if call_id:
            self.cards[call_id] = card

    def _on_failed(self, event: AgentEvent) -> None:
        self._chat().mount(Notice(f"error: {event.data.get('error', 'run failed')}", kind="error"))
        self._finish_run("failed")

    def _on_subagent_started(self, event: AgentEvent) -> None:
        child = str(event.data.get("child_run_id", ""))
        name = str(event.data.get("agent_name", "subagent"))
        if child:
            self._last_child_id = child
        row = SubagentRow(name)
        self._chat().mount(row)
        if child:
            self.subagent_rows[child] = row
            self._subagent_state[child] = (name, "running")
        self._refresh_sidebar()

    def _on_subagent_completed(self, event: AgentEvent) -> None:
        child = str(event.data.get("child_run_id", ""))
        status = str(event.data.get("status", "completed"))
        row = self.subagent_rows.get(child)
        if row is not None:
            row.finish(status)
        if child in self._subagent_state:
            self._subagent_state[child] = (self._subagent_state[child][0], status)
        self._refresh_sidebar()

    def _finish_run(self, outcome: str) -> None:
        if not self._run_active and outcome == "failed":
            return
        self._last_elapsed = time.monotonic() - self._run_started if self._run_started else 0.0
        self._last_outcome = "cancelled" if self._cancel_sent and outcome == "failed" else outcome
        self._run_active = False
        self._cancel_sent = False
        for card in self.cards.values():
            if card.status == "running":
                card.finish("done")
        self._refresh_all()

    # ── conversation helpers ──────────────────────────────────────────────────
    def _chat(self) -> VerticalScroll:
        return self.query_one("#chat", VerticalScroll)

    def _remove_welcome(self) -> None:
        """First content ends the empty state and starts following the output.

        The chat is anchored only once there is a conversation: an anchored container
        bottom-aligns short content, which would push the welcome panel to the bottom.
        """
        for welcome in self.query(Welcome):
            welcome.remove()
        chat = self._chat()
        if not chat.is_anchored:
            chat.anchor()  # follow new output unless the user scrolls up

    def _bump_tokens(self, text: str) -> None:
        """Approximate context usage: ~4 chars per token."""
        self._tokens += max(1, len(text) // 4)
        self._refresh_sidebar()

    # ── header / sidebar / hints ──────────────────────────────────────────────
    def _tick(self) -> None:
        now = time.monotonic()
        for card in self.cards.values():
            card.tick(now)
        for row in self.subagent_rows.values():
            row.tick(now)
        if self._run_active:
            self._refresh_header(now)

    def _refresh_all(self) -> None:
        if len(self.query(HeaderBar)) == 0:
            return  # fires in the gap between screen teardown and app unmount
        self._refresh_header()
        self._refresh_sidebar()
        self._refresh_hints()

    def _refresh_header(self, now: float | None = None) -> None:
        if len(self.query(HeaderBar)) == 0:
            return
        header = self.query_one(HeaderBar)
        header.set_title(self._task_title or "New conversation")
        now = time.monotonic() if now is None else now
        if self._run_active and self._cancel_sent:
            state = "[$warning]◌ cancelling…[/]"
        elif self._run_active:
            frame = _SPINNER[int(now * 10) % len(_SPINNER)]
            state = f"[$accent]{frame} working {format_duration(now - self._run_started)}[/]"
        elif self._last_outcome == "done":
            state = f"[$success]✓ done {format_duration(self._last_elapsed)}[/]"
        elif self._last_outcome == "cancelled":
            state = "[$warning]■ cancelled[/]"
        elif self._last_outcome == "failed":
            state = "[$error]✗ failed[/]"
        else:
            state = "[$text-muted]● idle[/]"
        header.set_state(state)

    def _sidebar_state(self) -> SidebarState:
        running = sum(1 for c in self.cards.values() if c.status == "running")
        if self._run_active:
            word = "cancelling" if self._cancel_sent else "working"
        else:
            word = self._last_outcome or "idle"
        return SidebarState(
            title=self._task_title or "New conversation",
            session=f"session {self._session_id or ''}",
            tokens=self._tokens,
            window=CONTEXT_WINDOW,
            tools_run=self._tools_run,
            tools_running=running,
            subagents=list(self._subagent_state.values()),
            workspace=_short_path(self.application.config.workspace, 28),
            branch=self._branch,
            state=word,
        )

    def _refresh_sidebar(self) -> None:
        if len(self.query("#sidebar")) == 0:
            return
        self.query_one(Sidebar).render_state(self._sidebar_state())

    def _refresh_hints(self) -> None:
        if len(self.query("#hints")) == 0:
            return
        model = self.application.config.model
        state = "esc to cancel" if self._run_active else "ctrl+j for a new line"
        self.query_one("#hints", Static).update(f"{model}  ·  {state}")
        prompt = self.query_one("#prompt", PromptArea)
        prompt.placeholder = "Working…" if self._run_active else "Ask anything…"

    # ── actions ──────────────────────────────────────────────────────────────
    async def action_cancel_run(self) -> None:
        if self._active_run_id is not None and self._run_active and not self._cancel_sent:
            self._cancel_sent = True
            self._refresh_all()
            await self.application.cancel_run(self._active_run_id)

    async def action_cancel_or_quit(self) -> None:
        if self._run_active and not self._cancel_sent:
            await self.action_cancel_run()
        else:
            self.exit()

    async def action_escape_or_focus(self) -> None:
        """Esc always returns control: cancel a live run, else focus the prompt."""
        if self._run_active and not self._cancel_sent:
            await self.action_cancel_run()
        else:
            self.query_one("#prompt", PromptArea).focus()

    def action_scroll_chat(self, key: str) -> None:
        chat = self._chat()
        if key == "pageup":
            chat.scroll_page_up()
        else:
            chat.scroll_page_down()

    def action_clear_chat(self) -> None:
        """New conversation: drop history, reset the title, counters and cards."""
        self._chat().release_anchor()
        self._chat().remove_children()
        self._chat().mount(Welcome())
        self.cards.clear()
        self.subagent_rows.clear()
        self._subagent_state.clear()
        self._tools_run = 0
        self._task_title = None
        self._tokens = 0
        self._last_outcome = ""
        self._refresh_all()

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
        if self._last_child_id is not None:
            await self.application.cancel_run(self._last_child_id)


def _short(text: str, width: int) -> str:
    """Truncate `text` to `width` columns with a trailing ellipsis."""
    return text if len(text) <= width else text[: width - 1] + "…"


def _short_path(path: Path, width: int) -> str:
    """Render a workspace path with a ``~/`` prefix when it sits under home."""
    try:
        rel = path.expanduser().resolve().relative_to(Path.home())
        text = "~/" + str(rel)
    except ValueError:
        text = str(path)
    return _short(text, width)


def _parse_delegation(text: str) -> tuple[str, str] | None:
    """Parse ``/agent <name> <task>`` or ``@<name> <task>`` into ``(name, task)``."""
    if text.startswith("/agent "):
        rest = text[len("/agent ") :].strip()
    elif text.startswith("@"):
        rest = text[1:].strip()
    else:
        return None
    parts = rest.split(maxsplit=1)
    if len(parts) < 2:
        return None
    return parts[0], parts[1]
