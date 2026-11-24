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
import contextlib
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar, cast

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import Hit, Hits, Provider
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Static

from agent_stack.app.application import Application, IdentityResolver
from agent_stack.core.events import AgentEvent, EventType
from agent_stack.gateways.base import MessageEnvelope, SessionUsage, usage_summary
from agent_stack.gateways.tui.render import (
    tool_args_lines,
    tool_detail,
)
from agent_stack.gateways.tui.theme import DARK, DEFAULT_THEME, LIGHT, THEMES
from agent_stack.gateways.tui.widgets import (
    AgentMessage,
    ApprovalScreen,
    ChangesScreen,
    Disclosure,
    HeaderBar,
    HelpScreen,
    Notice,
    PromptArea,
    RewindScreen,
    SessionPicker,
    Sidebar,
    SidebarState,
    SubagentRow,
    UserMessage,
    Welcome,
    format_duration,
    spinner_frame,
)

__all__ = ["TuiApp", "TuiCommands"]

_QUIT_WORDS = frozenset({"exit", "quit"})

#: Two Escapes within this many seconds count as "esc esc".
_DOUBLE_ESC_SECONDS = 0.8


#: Tools whose calls change files; their cards show a diff once they finish.
_FILE_TOOLS = frozenset({"write_file", "edit_file"})


class TuiCommands(Provider):
    """App commands surfaced in the Ctrl+P command palette."""

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        app = cast("TuiApp", self.app)
        commands: list[tuple[str, Any]] = [
            ("Cancel current run", app.action_cancel_run),
            ("New conversation", app.action_new_session),
            ("Switch conversation…", app.action_switch_session),
            ("Rewind to an earlier message…", app.action_rewind),
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


class TuiApp(App[None]):
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
        Binding("ctrl+n", "new_session", "new"),
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
    ) -> None:
        super().__init__()
        self.application = application
        self._principal = IdentityResolver().resolve("tui", principal)
        self._session_id = session_id

        for theme in THEMES:
            self.register_theme(theme)
        wanted = application.config.theme
        self.theme = wanted if wanted in self.available_themes else DEFAULT_THEME

        self._last_escape = 0.0
        #: The conversation whose file state the folder was last put in (None: unknown).
        self._folder_state: str | None = session_id
        self._active_run_id: str | None = None
        self._run_active = False
        self._cancel_sent = False
        self._run_started = 0.0
        self._last_outcome = ""  # "" | "done" | "failed" | "cancelled"
        self._last_elapsed = 0.0
        self._task_title: str | None = None
        self._tokens = 0
        self._usage = SessionUsage()  # what this conversation has cost so far (all runs)
        self._file_paths: dict[str, str] = {}  # write/edit call id -> the path it touches
        self._branch = ""
        self._sidebar_override: bool | None = None
        self._last_child_id: str | None = None

        self.cards: dict[str, Disclosure] = {}
        self.subagent_rows: dict[str, SubagentRow] = {}
        self._tools_run = 0
        self._titles: dict[str, str] = {}
        self._awaiting = 0  # approval requests currently open
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
        if self._busy():
            return
        self.query_one("#prompt", PromptArea).clear_text()
        await self._submit_prompt(text)

    async def _submit_prompt(self, text: str) -> None:
        self._remove_welcome()
        if self._session_id is None:
            # A launch (or ctrl+n) is only a draft: the session is stored on first message,
            # so opening and quitting never leaves an empty conversation behind.
            self._session_id = await self.application.new_session(self._principal)
        if self._task_title is None:
            self._task_title = text
            await self._name_session(text)
        self._bump_tokens(text)
        self._chat().mount(UserMessage(text))
        delegated = _parse_delegation(text)
        if delegated is not None:
            # Honest delegation: the app can only start a run on the wired agent, so the
            # named subagent is reached by asking the model to delegate to it (P20).
            name, task = delegated
            text = f"Delegate this task to the `{name}` subagent: {task}"
        await self._start_run(text)

    def _busy(self) -> bool:
        """True (after telling the user) while a run is active: one thing at a time."""
        if self._run_active:
            self.notify("A run is in progress. Press esc to cancel it first.", severity="warning")
        return self._run_active

    async def _name_session(self, text: str) -> None:
        """Persist the first message as the session's title so it is findable later."""
        try:
            await self.application.set_session_title(self._session_id or "", text[:80])
        except Exception:  # a cosmetic title must never block the message
            return

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
            if self._run_active and self._active_run_id == handle.run_id:
                # The stream ended with no terminal event: a hard cancel publishes none.
                self._finish_run("failed")
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
        if event.type is EventType.MODEL_USAGE:
            # Every run counts, a subagent's included: they are all this conversation's spend.
            self._usage = self._usage.plus(event.data)
            self._refresh_all()
            return
        if event.type is EventType.APPROVAL_REQUIRED:
            self._on_approval_required(event)  # from any run: a child can be blocked too
        elif event.type is EventType.APPROVAL_RESOLVED:
            self._on_approval_resolved(event)
        elif event.type is EventType.SUBAGENT_STARTED:
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
            call_id = str(event.data.get("tool_call_id"))
            card = self.cards.get(call_id)
            if card is not None:
                ok = event.data.get("ok", True)  # older events carry no flag
                if ok and call_id in self._file_paths:
                    # settle after the diff arrives, so the card opens on what changed
                    self._spawn(self._finish_with_diff(card, call_id, event))
                else:
                    card.finish("done" if ok else "failed")
        elif etype is EventType.RUN_FAILED:
            self._on_failed(event)
        elif etype is EventType.RUN_COMPLETED:
            self._finish_run("done")  # a successful run adds no row to the conversation
        if etype is not EventType.TOOL_OUTPUT:  # output only moves the card; the tick repaints it
            self._refresh_all()

    def _on_approval_required(self, event: AgentEvent) -> None:
        """Pause on the card and ask the human; the answer goes back through the Application."""
        call_id = str(event.data.get("tool_call_id", ""))
        tool = str(event.data.get("tool", "tool"))
        args = event.data.get("args")
        card = self.cards.get(call_id)
        if card is not None:
            card.await_approval()
        self._awaiting += 1
        detail = tool_detail(args) if tool == "run_command" else tool_args_lines(args)
        approval_id = str(event.data.get("approval_id", ""))

        def answer(approved: bool | None) -> None:
            if not self._run_active:
                return  # the run ended (cancelled) while the dialog was open
            try:
                self.application.decide_approval(
                    approval_id,
                    run_id=event.run_id,
                    session_id=event.session_id,
                    principal=self._principal,
                    approve=bool(approved),
                )
            except Exception as exc:  # a refused decision must not take the UI down
                self.notify(str(exc), severity="warning")

        self.push_screen(ApprovalScreen(tool, detail), answer)
        self._refresh_all()

    def _on_approval_resolved(self, event: AgentEvent) -> None:
        self._awaiting = max(0, self._awaiting - 1)
        card = self.cards.get(str(event.data.get("tool_call_id", "")))
        if card is not None and event.data.get("status") == "approved":
            card.resume_running()
        self._refresh_all()

    def _close_approval_dialogs(self) -> None:
        """Dismiss any open approval dialog (the run it belonged to is over)."""
        while isinstance(self.screen, ApprovalScreen):
            self.screen.dismiss(False)

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
        name = str(event.data.get("tool", "tool"))
        args = event.data.get("args")
        card = Disclosure(
            "tool",
            name,
            tool_detail(args),
            body=tool_args_lines(args),
            running=True,
            opened=name == "run_command",
        )
        self._remove_welcome()
        self._chat().mount(card)
        self._tools_run += 1
        call_id = str(event.data.get("tool_call_id", ""))
        if call_id:
            self.cards[call_id] = card
            path = args.get("path") if isinstance(args, Mapping) else None
            if name in _FILE_TOOLS and isinstance(path, str):
                self._file_paths[call_id] = path

    def _on_failed(self, event: AgentEvent) -> None:
        self._chat().mount(Notice(f"error: {event.data.get('error', 'run failed')}"))
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
        self._refresh_sidebar()

    def _on_subagent_completed(self, event: AgentEvent) -> None:
        child = str(event.data.get("child_run_id", ""))
        status = str(event.data.get("status", "completed"))
        row = self.subagent_rows.get(child)
        if row is not None:
            row.finish(status)
        self._refresh_sidebar()

    def _finish_run(self, outcome: str) -> None:
        if not self._run_active and outcome == "failed":
            return
        self._close_approval_dialogs()
        self._awaiting = 0
        self._last_elapsed = time.monotonic() - self._run_started if self._run_started else 0.0
        self._last_outcome = "cancelled" if self._cancel_sent and outcome == "failed" else outcome
        self._run_active = False
        self._cancel_sent = False
        if self._session_id is not None:
            self._folder_state = self._session_id  # this conversation's edits are what is on disk
        for card in self.cards.values():
            if card.status == "running":
                card.finish("done")
            elif card.status == "waiting":  # the run ended while it was asking
                card.finish("failed")
        self._refresh_all()
        self._spawn(self._sync_usage())

    async def _finish_with_diff(self, card: Disclosure, call_id: str, event: AgentEvent) -> None:
        """Fill a write/edit card with the diff of what that call changed, then settle it."""
        path = self._file_paths.pop(call_id, "")
        try:
            change = await self.application.changed_file(
                event.session_id, path, event.run_id, latest_only=True
            )
        except Exception:  # a diff is a nicety: the card must settle either way
            change = None
        if change is not None:
            card.show_diff(change.diff or "(no net change)")
        card.finish("done")
        self._refresh_all()

    async def action_show_changes(self) -> None:
        """Open the list of files the agent created or edited in this conversation."""
        if self._session_id is None:
            self.notify("Nothing has been changed yet.", severity="information")
            return
        try:
            files = await self.application.changed_files(self._session_id)
        except Exception as exc:
            self.notify(f"Could not read the changes: {exc}", severity="error")
            return
        self.push_screen(ChangesScreen(files))

    async def _sync_usage(self) -> None:
        """Replace the running total with the durable one (it also holds subagent runs)."""
        session_id = self._session_id
        if session_id is None:
            return
        try:
            usage = await self.application.session_usage(session_id)
        except Exception:  # a cost display must never break the conversation
            return
        if self._session_id == session_id:
            self._usage = usage
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
        elif self._run_active and self._awaiting:
            state = "[$warning]? needs your approval[/]"
        elif self._run_active:
            state = f"[$accent]{spinner_frame(now)} working {format_duration(now - self._run_started)}[/]"
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
            word = (
                "cancelling"
                if self._cancel_sent
                else "awaiting approval"
                if self._awaiting
                else "working"
            )
        else:
            word = self._last_outcome or "idle"
        return SidebarState(
            title=self._task_title or "New conversation",
            session=f"session {self._session_id}" if self._session_id else "not saved yet",
            tokens=self._tokens,
            window=self.application.config.context_window,
            tools_run=self._tools_run,
            tools_running=running,
            subagents=[(r.agent_name, r.outcome) for r in self.subagent_rows.values()],
            workspace=_short_path(self.application.config.workspace, 28),
            branch=self._branch,
            state=word,
            cost=usage_summary(self._usage) if self._usage.calls else "",
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
        spent = f"  ·  {usage_summary(self._usage)}" if self._usage.calls else ""
        self.query_one("#hints", Static).update(f"{model}{spent}  ·  {state}")
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
        """Esc returns control: cancel a live run, else focus the prompt; esc esc rewinds.

        A second Esc within a moment clears what is typed, or, with an empty prompt, opens the
        rewind picker (the Claude Code habit).
        """
        if self._run_active and not self._cancel_sent:
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

    async def action_rewind(self) -> None:
        """Pick an earlier message and go back to it (conversation, code, or both)."""
        if self._busy():
            return
        if self._session_id is None:
            self.notify("Nothing to rewind to yet.")
            return
        try:
            turns = await self.application.timeline.lineage(self._session_id)
        except Exception as exc:  # a conversation with no event log, say
            self.notify(f"Cannot rewind here: {exc}", severity="error")
            return
        turns = [t for t in turns if t.turn.user_text]
        if not turns:
            self.notify("Nothing to rewind to yet.")
            return
        self.push_screen(RewindScreen(turns), self._on_rewind_picked)

    async def action_restore_files(self) -> None:
        """Put the folder in this conversation's file state (after opening another one)."""
        if self._busy() or self._session_id is None:
            return
        source = self._folder_state or self._session_id
        try:
            result = await self.application.timeline.switch_files(source, self._session_id)
        except Exception as exc:
            self.notify(f"Could not restore files: {exc}", severity="error")
            return
        if not result.conflicts:
            self._folder_state = self._session_id
        moved = len(result.moved)
        text = (
            f"{moved} file{'s' if moved != 1 else ''} restored."
            if moved
            else "Files already match."
        )
        if result.conflicts:
            text += f" Left alone (changed since): {', '.join(result.conflicts)}"
        self.notify(text, severity="warning" if result.conflicts else "information")

    async def _warn_if_files_differ(self, session_id: str) -> None:
        """After opening a conversation: say so when the folder is in another one's state."""
        source = self._folder_state
        if source is None or source == session_id:
            return
        try:
            result = await self.application.timeline.switch_files(source, session_id, dry_run=True)
        except Exception:
            return
        if result.moved or result.conflicts:
            self.notify(
                "The files in the folder match another conversation. "
                "ctrl+p, then 'Put this conversation's files in the folder' to match this one.",
                severity="warning",
                timeout=10,
            )

    def _on_rewind_picked(self, choice: tuple[Any, str] | None) -> None:
        if choice is not None:
            self._spawn(self._rewind(*choice))

    async def _rewind(self, target: Any, mode: str) -> None:
        current = self._session_id
        if current is None:
            return
        try:
            outcome = await self.application.timeline.rewind_to(
                current,
                target,
                conversation=mode in ("both", "conversation"),
                code=mode in ("both", "code"),
            )
        except Exception as exc:
            self.notify(f"Rewind failed: {exc}", severity="error")
            return
        if mode in ("both", "code") and not outcome.files.conflicts:
            self._folder_state = outcome.session_id
        if outcome.session_id != current:
            self._titles[outcome.session_id] = "rewound"
            await self._open_session(outcome.session_id)
        moved = len(outcome.files.moved)
        text = (
            f"Rewound. {moved} file{'s' if moved != 1 else ''} restored." if moved else "Rewound."
        )
        if outcome.files.conflicts:
            text += f" Left alone (changed since): {', '.join(outcome.files.conflicts)}"
        self.notify(text, severity="warning" if outcome.files.conflicts else "information")
        if mode in ("both", "conversation"):
            prompt = self.query_one("#prompt", PromptArea)
            prompt.load_text(outcome.prompt)  # edit it and send again
            prompt.focus()

    def action_scroll_chat(self, key: str) -> None:
        chat = self._chat()
        if key == "pageup":
            chat.scroll_page_up()
        else:
            chat.scroll_page_down()

    async def _reset_conversation(self) -> None:
        """Empty the screen and per-conversation counters (the stored session is untouched)."""
        chat = self._chat()
        chat.release_anchor()
        await chat.remove_children()  # awaited: the old Welcome shares its id
        await chat.mount(Welcome())
        self.cards.clear()
        self.subagent_rows.clear()
        self._tools_run = 0
        self._task_title = None
        self._tokens = 0
        self._usage = SessionUsage()
        self._last_outcome = ""
        self._last_child_id = None

    async def action_new_session(self) -> None:
        """Start a fresh stored conversation; the current one stays in the picker."""
        if self._busy():
            return
        self._session_id = None  # a draft; stored when the first message is sent
        await self._reset_conversation()
        self._refresh_all()
        self.query_one("#prompt", PromptArea).focus()

    async def action_switch_session(self) -> None:
        """Open the conversation picker."""
        if self._busy():
            return
        sessions = await self.application.list_sessions(self._principal)
        self._titles = {s.id: s.title for s in sessions}
        self.push_screen(
            SessionPicker(sessions, self._session_id, self._delete_session),
            self._on_session_picked,
        )

    async def _delete_session(self, session_id: str) -> None:
        """Delete a stored conversation; deleting the open one returns to a fresh draft."""
        await self.application.delete_session(session_id)
        self._titles.pop(session_id, None)
        if session_id == self._session_id:
            self._session_id = None
            await self._reset_conversation()
            self._refresh_all()
        self.notify("Conversation deleted.")

    def _on_session_picked(self, session_id: str | None) -> None:
        if session_id is not None and session_id != self._session_id:
            self._spawn(self._open_session(session_id))

    async def _open_session(self, session_id: str) -> None:
        """Switch to a stored conversation and replay its history into the chat."""
        history = await self.application.session_history(session_id)
        self._session_id = session_id
        await self._reset_conversation()
        with contextlib.suppress(Exception):  # the cost is informational; history matters more
            self._usage = await self.application.session_usage(session_id)
        title = self._titles.get(session_id, "")
        self._task_title = None if title in ("", "Untitled") else title
        chat = self._chat()
        if history:
            self._remove_welcome()
        for item in history:
            if item.kind == "user":
                chat.mount(UserMessage(item.text))
            elif item.kind == "assistant":
                chat.mount(AgentMessage(item.text))
            elif item.kind == "tool":
                chat.mount(
                    Disclosure(
                        "tool",
                        item.text,
                        tool_detail(item.args),
                        body=tool_args_lines(item.args),
                    )
                )
            self._tokens += max(1, len(item.text) // 4)
        self._refresh_all()
        self.call_after_refresh(chat.scroll_end, animate=False)
        self.query_one("#prompt", PromptArea).focus()
        await self._warn_if_files_differ(session_id)

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
