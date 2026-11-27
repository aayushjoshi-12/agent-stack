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
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar, cast

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import Hit, Hits, Provider
from textual.containers import Horizontal, Vertical
from textual.markup import escape
from textual.widgets import ContentSwitcher, Footer, OptionList
from textual.widgets.option_list import Option

from agent_stack.app.application import Application, IdentityResolver
from agent_stack.core.events import AgentEvent, EventType
from agent_stack.gateways.base import MessageEnvelope, usage_summary
from agent_stack.gateways.tui.render import (
    tool_args_lines,
    tool_detail,
)
from agent_stack.gateways.tui.tabs import Conversation, TabBar, ellipsize
from agent_stack.gateways.tui.theme import DARK, DEFAULT_THEME, LIGHT, THEMES
from agent_stack.gateways.tui.widgets import (
    AgentMessage,
    ApprovalScreen,
    BranchesScreen,
    ChangesScreen,
    Disclosure,
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

#: ``/name`` or ``/name args``, but not a path such as ``/etc/hosts``.
_COMMAND_RE = re.compile(r"^/[A-Za-z][\w-]*(\s|$)")

#: Two Escapes within this many seconds count as "esc esc".
_DOUBLE_ESC_SECONDS = 0.8

#: Tools whose calls change files; their cards show a diff once they finish.
_FILE_TOOLS = frozenset({"write_file", "edit_file"})

#: Columns a tab's title gets in the tab bar before it ends in "...".
_TAB_TITLE = 24


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

    async def _resume_session(self, session_id: str) -> None:
        session = await self.application.get_session(session_id)
        if session is None:
            self.notify(f"No conversation {session_id}.", severity="error")
            self.tab.session_id = None
            return
        self._titles[session_id] = session.title or ""
        await self._open_session(session_id, here=True)

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

    # ── tabs ──────────────────────────────────────────────────────────────────
    def _new_conversation(self) -> Conversation:
        self._tab_seq += 1
        return Conversation(id=f"chat-{self._tab_seq}")

    def _show_tab(self, tab: Conversation) -> None:
        """Put ``tab`` on screen; the prompt keeps each tab's unsent text."""
        prompt = self.query_one("#prompt", PromptArea)
        if tab is not self.tab:
            self.tab.draft = prompt.text
            prompt.load_text(tab.draft)
            prompt.move_cursor(prompt.document.end)
        self.tab = tab
        self.query_one("#chats", ContentSwitcher).current = tab.id
        self._refresh_all()
        prompt.focus()

    async def _add_tab(self) -> Conversation:
        tab = self._new_conversation()
        tab.display = False  # the switcher shows it once it is current
        await self.query_one("#chats", ContentSwitcher).mount(tab)
        self._tabs.append(tab)
        self._show_tab(tab)
        return tab

    async def _replace_tab(self, old: Conversation) -> Conversation:
        """A fresh, empty pane in ``old``'s place (the stored conversation is untouched)."""
        tab = self._new_conversation()
        tab.display = False
        await self.query_one("#chats", ContentSwitcher).mount(tab, after=old)
        self._tabs[self._tabs.index(old)] = tab
        self._show_tab(tab)
        await old.remove()
        return tab

    async def action_new_session(self) -> None:
        """Open a new tab with a fresh conversation (stored when its first message is sent)."""
        if self.tab.blank:
            self.query_one("#prompt", PromptArea).focus()
            return
        await self._add_tab()

    async def action_close_tab(self) -> None:
        """Close this tab; its conversation stays stored and can be reopened (ctrl+o)."""
        if self._busy():
            return
        old = self.tab
        if len(self._tabs) == 1:
            await self._replace_tab(old)
            return
        at = self._tabs.index(old)
        self._tabs.remove(old)
        self._show_tab(self._tabs[min(at, len(self._tabs) - 1)])
        await old.remove()

    def action_go_to_tab(self, index: int) -> None:
        if 0 <= index < len(self._tabs):
            self._show_tab(self._tabs[index])

    def action_next_tab(self) -> None:
        self.action_go_to_tab((self._tabs.index(self.tab) + 1) % len(self._tabs))

    def action_previous_tab(self) -> None:
        self.action_go_to_tab((self._tabs.index(self.tab) - 1) % len(self._tabs))

    async def on_tab_bar_picked(self, message: TabBar.Picked) -> None:
        if message.index < 0:
            await self.action_new_session()
        else:
            self.action_go_to_tab(message.index)

    # ── slash commands ────────────────────────────────────────────────────────
    def _builtins(self) -> dict[str, tuple[str, Any]]:
        return {
            "new": ("start a new conversation in a new tab", self.action_new_session),
            "close": ("close this tab (the conversation stays stored)", self.action_close_tab),
            "sessions": ("open an earlier conversation", self.action_switch_session),
            "rewind": ("go back to an earlier message", self.action_rewind),
            "branches": ("draw this conversation's branches", self.action_show_branches),
            "ref": ("copy a reference to this point (or /ref <reference> to open one)", None),
            "files": ("put this conversation's files in the folder", self.action_restore_files),
            "mode": ("approval mode: /mode auto or /mode ask", None),
            "model": ("show or switch the model: /model provider:name", None),
            "compact": ("summarise the conversation to free context: /compact [focus]", None),
            "help": ("keyboard shortcuts", self.action_help),
            "quit": ("leave (prints how to continue)", self.action_quit_app),
        }

    def command_entries(self) -> list[tuple[str, str, str]]:
        """Every ``/command`` as ``(name, description, kind)``: built-ins first, then skills."""
        builtins = self._builtins()
        entries = [(name, desc, "builtin") for name, (desc, _) in builtins.items()]
        entries += [
            (c.name, c.description, "skill")
            for c in self.application.slash_commands()
            if c.name not in builtins
        ]
        return entries

    def on_text_area_changed(self, event: Any) -> None:
        """Show the ``/`` completion list while a command name is being typed."""
        popup = self.query_one("#completions", OptionList)
        text = event.text_area.text
        if (
            event.text_area.id != "prompt"
            or not text.startswith("/")
            or any(c.isspace() for c in text)
        ):
            popup.display = False
            return
        query = text[1:].lower()
        matches = [e for e in self.command_entries() if e[0].lower().startswith(query)]
        matches += [e for e in self.command_entries() if query in e[0].lower() and e not in matches]
        if not matches:
            popup.display = False
            return
        popup.clear_options()
        popup.add_options(
            [
                Option(
                    f"[bold $accent]/{name}[/]  [$foreground-muted]{escape(' '.join(desc.split())[:52])}"
                    f"{'  ·skill' if kind == 'skill' else ''}[/]",
                    id=name,
                )
                for name, desc, kind in matches[:30]
            ]
        )
        popup.highlighted = 0
        popup.display = True

    async def action_quit_app(self) -> None:
        self.exit()

    async def _run_slash(self, text: str) -> bool:
        """Handle ``/name args`` if it is a built-in; True when it was (skills go to the model)."""
        head, _, argument = text.strip().partition(" ")
        name, argument = head[1:].lower(), argument.strip()
        if name == "ref":
            if argument.startswith("asref://"):
                await self._open_reference(argument)
            else:
                await self.action_make_reference()
            return True
        if name == "mode":
            await self.action_toggle_approval_mode(argument or None)
            return True
        if name == "model":
            self._switch_model(argument)
            return True
        if name == "compact":
            self._spawn(self._compact(argument))  # waits for a running turn; keep the UI live
            return True
        entry = self._builtins().get(name)
        if entry is None:
            return False
        outcome = entry[1]()
        if outcome is not None:  # most actions are coroutines; help is plain
            await outcome
        return True

    async def _compact(self, focus: str) -> None:
        """``/compact [focus]``: replace the conversation so far with a saved summary."""
        if self.tab.session_id is None:
            self.notify("Nothing to compact yet.")
            return
        self.notify("Compacting the conversation…")
        try:
            count = await self.application.compact_session(self.tab.session_id, focus)
        except Exception as exc:
            self.notify(f"Cannot compact: {exc}", severity="error")
            return
        self.notify(
            f"Compacted {count} messages into a summary; the next message starts from it."
            if count
            else "Nothing new to compact."
        )

    def _switch_model(self, wanted: str) -> None:
        """``/model`` shows the model; ``/model provider:name`` switches it for the next runs."""
        if not wanted:
            self.notify(f"Model: {self.application.get_model()}. Switch: /model provider:name")
            return
        try:
            now = self.application.set_model(wanted)
        except Exception as exc:
            self.notify(f"Cannot switch model: {exc}", severity="error")
            return
        self.notify(f"Model: {now} (from the next message on)")
        self._refresh_all()

    # ── approval mode ─────────────────────────────────────────────────────────
    async def action_toggle_approval_mode(self, wanted: str | None = None) -> None:
        """Flip who answers tool approvals first: you (ask) or the classifier (auto)."""
        current = self.application.get_approval_mode()
        target = wanted or ("auto" if current == "ask" else "ask")
        try:
            now = self.application.set_approval_mode(target)
        except Exception as exc:
            self.notify(f"Cannot switch mode: {exc}", severity="error")
            return
        text = (
            "Auto mode: a classifier decides what is safe; you are asked only when it is unsure."
            if now == "auto"
            else "Ask mode: you approve every gated action."
        )
        self.notify(text, severity="warning" if now == "auto" else "information", timeout=6)
        self._refresh_all()

    # ── input ─────────────────────────────────────────────────────────────────
    async def on_prompt_area_submitted(self, event: PromptArea.Submitted) -> None:
        text = event.text
        if text in _QUIT_WORDS:
            self.exit()
            return
        command = bool(_COMMAND_RE.match(text.strip()))
        if self.tab.running and not command:
            await self._queue_input(text)
            return
        prompt = self.query_one("#prompt", PromptArea)
        if command and text.strip()[1:].split()[0].lower() in self._builtins():
            self.query_one("#completions", OptionList).display = False
            prompt.clear_text()
            await self._run_slash(text)
            return
        if self._busy():
            return
        self.query_one("#completions", OptionList).display = False
        prompt.clear_text()
        if command:
            names = {c.name.lower().replace("_", "-") for c in self.application.slash_commands()}
            if text.strip()[1:].split()[0].lower().replace("_", "-") not in names:
                self.notify(
                    f"Unknown command {text.split()[0]}. Type / to see them.", severity="warning"
                )
                return
        if text.strip().startswith("asref://"):  # a pasted reference opens; it is not a prompt
            await self._open_reference(text.strip())
            return
        await self._submit_prompt(text)

    async def _open_reference(self, reference: str) -> None:
        """Open a point shared from another conversation as a new one."""
        try:
            opened = await self.application.timeline.open_reference(reference)
        except Exception as exc:
            self.notify(f"Cannot open that reference: {exc}", severity="error")
            return
        self._titles[opened] = "from a reference"
        await self._open_session(opened)  # also warns when the folder holds other files
        self.notify("Opened from a reference. Its history is above; continue from here.")

    async def action_show_branches(self) -> None:
        """Draw the branches of the open conversation."""
        if self.tab.session_id is None:
            self.notify("Nothing to show yet.")
            return
        try:
            text = await self.application.timeline.tree_text(self.tab.session_id)
        except Exception as exc:
            self.notify(f"Cannot draw the branches: {exc}", severity="error")
            return
        self.push_screen(BranchesScreen(text))

    async def action_make_reference(self) -> None:
        """Name the last finished turn so another conversation can open it."""
        session_id = self.tab.session_id
        if session_id is None:
            self.notify("Nothing to reference yet.")
            return
        try:
            timeline = self.application.timeline
            done = [t for t in await timeline.turns(session_id) if t.completed_event_id]
            if not done:
                self.notify("Nothing has finished in this conversation yet.")
                return
            reference = await timeline.reference(session_id, str(done[-1].index))
        except Exception as exc:
            self.notify(f"Cannot make a reference: {exc}", severity="error")
            return
        self.copy_to_clipboard(reference)
        self.notify(
            f"Copied. Paste it into another conversation's prompt and press enter:\n{reference}",
            timeout=15,
        )

    async def _submit_prompt(self, text: str) -> None:
        tab = self.tab
        self._remove_welcome(tab)
        if tab.session_id is None:
            # A new tab is only a draft: the session is stored on first message, so opening
            # and quitting never leaves an empty conversation behind.
            tab.session_id = await self.application.new_session(self._principal)
        if tab.title is None:
            tab.title = text
            await self._name_session(tab.session_id, text)
        self._bump_tokens(tab, text)
        tab.mount(UserMessage(text))
        delegated = _parse_delegation(text)
        if delegated is not None:
            # Honest delegation: the app can only start a run on the wired agent, so the
            # named subagent is reached by asking the model to delegate to it (P20).
            name, task = delegated
            text = f"Delegate this task to the `{name}` subagent: {task}"
        await self._start_run(tab, text)

    async def _queue_input(self, text: str) -> None:
        """Send a message into the working run; the model reads it before its next step."""
        tab = self.tab
        if not await self.application.send_input(tab.run_id or "", text):
            self.notify("The run had just finished. Press enter to send it.", severity="warning")
            return
        self.query_one("#prompt", PromptArea).clear_text()
        bubble = UserMessage(text, queued=True)
        tab.queued.append(bubble)
        tab.mount(bubble)

    async def action_edit_queued(self) -> None:
        """Take the latest queued message back into the prompt (edit it, or clear it to drop it)."""
        tab = self.tab
        if not tab.queued:
            return
        bubble = tab.queued[-1]
        if not await self.application.withdraw_input(tab.run_id or "", bubble.text):
            self.notify("The agent has already read that message.")
            return
        tab.queued.remove(bubble)
        await bubble.remove()
        prompt = self.query_one("#prompt", PromptArea)
        prompt.load_text(bubble.text)
        prompt.move_cursor(prompt.document.end)

    def _busy(self) -> bool:
        """True (after telling the user) while this tab's run is active."""
        if self.tab.running:
            self.notify("A run is in progress. Press esc to cancel it first.", severity="warning")
        return self.tab.running

    async def _name_session(self, session_id: str, text: str) -> None:
        """Persist the first message as the session's title so it is findable later."""
        try:
            await self.application.set_session_title(session_id, text[:80])
        except Exception:  # a cosmetic title must never block the message
            return

    async def _start_run(self, tab: Conversation, text: str) -> None:
        envelope = MessageEnvelope(
            principal=self._principal, session=tab.session_id or "", text=text
        )
        handle = await self.application.send_message(envelope)
        tab.run_id = handle.run_id
        tab.running = True
        tab.cancel_sent = False
        tab.run_started = time.monotonic()
        self._refresh_all()
        self._spawn(self._consume(tab, handle))

    async def _consume(self, tab: Conversation, handle: Any) -> None:
        try:
            async for event in handle.events():
                self.handle_event(event, tab)
            if tab.running and tab.run_id == handle.run_id:
                # The stream ended with no terminal event: a hard cancel publishes none.
                self._finish_run(tab, "failed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a broken run must not tear down the UI
            self.notify(str(exc), severity="error")
            self._finish_run(tab, "failed")

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ── event routing ─────────────────────────────────────────────────────────
    def handle_event(self, event: AgentEvent, tab: Conversation | None = None) -> None:
        """Route one runtime event to its tab's conversation, the tab bar and the sidebar."""
        tab = tab or self.tab
        if not tab.is_attached:
            # The tab was closed, or the screen torn down (quit or test teardown), while a
            # stream was still live; drop the event rather than touching dead widgets.
            return
        if event.type is EventType.MODEL_USAGE:
            # Every run counts, a subagent's included: they are all this conversation's spend.
            tab.usage = tab.usage.plus(event.data)
            if event.run_id == tab.run_id and event.data.get("purpose") != "title":
                tab.context = int(event.data.get("input_tokens", 0) or 0) or tab.context
            self._refresh_all()
            return
        if event.type is EventType.APPROVAL_REQUIRED:
            self._on_approval_required(tab, event)  # from any run: a child can be blocked too
        elif event.type is EventType.APPROVAL_RESOLVED:
            self._on_approval_resolved(tab, event)
        elif event.type is EventType.APPROVAL_AUTO:
            self._on_approval_auto(event)
        elif event.type is EventType.SUBAGENT_STARTED:
            self._on_subagent_started(tab, event)
        elif event.type is EventType.SUBAGENT_COMPLETED:
            self._on_subagent_completed(tab, event)
        elif event.type is EventType.CONTEXT_COMPACTED and event.parent_run_id is None:
            tab.mount(
                Notice(f"context compacted: {event.data.get('messages', 0)} messages summarised")
            )
        elif event.parent_run_id is not None and event.run_id != tab.run_id:
            pass  # child-run detail stays out of the parent conversation
        else:
            self._on_main_event(tab, event)

    def _on_main_event(self, tab: Conversation, event: AgentEvent) -> None:
        etype = event.type
        if etype is EventType.RUN_STARTED:
            tab.run_id = event.run_id
            if not tab.running:
                tab.running = True
                tab.run_started = time.monotonic()
        elif etype is EventType.MODEL_REASONING:
            self._on_reasoning(tab, event)
        elif etype is EventType.ASSISTANT_MESSAGE:
            self._on_assistant(tab, event)
        elif etype is EventType.TOOL_STARTED:
            self._on_tool_started(tab, event)
        elif etype is EventType.TOOL_OUTPUT:
            card = tab.cards.get(str(event.data.get("tool_call_id")))
            if card is not None:
                card.append(str(event.data.get("chunk", "")))
        elif etype is EventType.TOOL_COMPLETED:
            call_id = str(event.data.get("tool_call_id"))
            card = tab.cards.get(call_id)
            if card is not None:
                ok = event.data.get("ok", True)  # older events carry no flag
                if ok and call_id in tab.file_paths:
                    # settle after the diff arrives, so the card opens on what changed
                    self._spawn(self._finish_with_diff(tab, card, call_id, event))
                else:
                    card.finish("done" if ok else "failed")
        elif etype is EventType.INPUT_RECEIVED:
            self._on_input_received(tab, str(event.data.get("text", "")))
        elif etype is EventType.RUN_FAILED:
            self._on_failed(tab, event)
        elif etype is EventType.RUN_COMPLETED:
            self._finish_run(tab, "done")  # a successful run adds no row to the conversation
        if etype is not EventType.TOOL_OUTPUT:  # output only moves the card; the tick repaints it
            self._refresh_all()

    def _on_approval_required(self, tab: Conversation, event: AgentEvent) -> None:
        """Pause on the card and ask the human; the answer goes back through the Application."""
        call_id = str(event.data.get("tool_call_id", ""))
        tool = str(event.data.get("tool", "tool"))
        args = event.data.get("args")
        card = tab.cards.get(call_id)
        if card is not None:
            card.await_approval()
        tab.awaiting += 1
        detail = tool_detail(args) if tool == "run_command" else tool_args_lines(args)
        approval_id = str(event.data.get("approval_id", ""))

        def answer(approved: bool | None) -> None:
            if screen in tab.dialogs:
                tab.dialogs.remove(screen)
            if not tab.running:
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

        if tab is not self.tab:
            self._show_tab(tab)  # what is being approved is on screen behind the question
        screen = ApprovalScreen(tool, detail, str(event.data.get("note", "")))
        tab.dialogs.append(screen)
        self.push_screen(screen, answer)
        self._refresh_all()

    def _on_approval_auto(self, event: AgentEvent) -> None:
        """Say what auto mode decided, so nothing is approved or blocked silently."""
        verdict = str(event.data.get("verdict"))
        tool = str(event.data.get("tool", "tool"))
        reason = str(event.data.get("reason", ""))
        if verdict == "allow":
            self.notify(f"auto-approved {tool}: {reason}", timeout=3)
        elif verdict == "deny":
            self.notify(f"auto mode blocked {tool}: {reason}", severity="error", timeout=10)
        # "ask" is followed by the usual question, which shows the reason

    def _on_approval_resolved(self, tab: Conversation, event: AgentEvent) -> None:
        tab.awaiting = max(0, tab.awaiting - 1)
        card = tab.cards.get(str(event.data.get("tool_call_id", "")))
        if card is not None and event.data.get("status") == "approved":
            card.resume_running()
        self._refresh_all()

    def _close_approval_dialogs(self, tab: Conversation) -> None:
        """Dismiss the tab's open approval dialogs (the run they belonged to is over)."""
        while self.screen in tab.dialogs:
            self.screen.dismiss(False)
        tab.dialogs.clear()

    def _on_reasoning(self, tab: Conversation, event: AgentEvent) -> None:
        text = str(event.data.get("text", ""))
        if not text.strip():
            return
        self._bump_tokens(tab, text)
        self._remove_welcome(tab)
        tab.mount(Disclosure("think", "thinking", body=text))

    def _on_assistant(self, tab: Conversation, event: AgentEvent) -> None:
        content = str(event.data.get("content", ""))
        if not content.strip():
            return
        self._bump_tokens(tab, content)
        self._remove_welcome(tab)
        tab.mount(AgentMessage(content))

    def _on_tool_started(self, tab: Conversation, event: AgentEvent) -> None:
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
        self._remove_welcome(tab)
        tab.mount(card)
        tab.tools_run += 1
        call_id = str(event.data.get("tool_call_id", ""))
        if call_id:
            tab.cards[call_id] = card
            path = args.get("path") if isinstance(args, Mapping) else None
            if name in _FILE_TOOLS and isinstance(path, str):
                tab.file_paths[call_id] = path

    def _on_failed(self, tab: Conversation, event: AgentEvent) -> None:
        tab.mount(Notice(f"error: {event.data.get('error', 'run failed')}"))
        self._finish_run(tab, "failed")

    def _on_subagent_started(self, tab: Conversation, event: AgentEvent) -> None:
        child = str(event.data.get("child_run_id", ""))
        name = str(event.data.get("agent_name", "subagent"))
        if child:
            tab.last_child_id = child
        row = SubagentRow(name)
        tab.mount(row)
        if child:
            tab.subagent_rows[child] = row
        self._refresh_sidebar()

    def _on_subagent_completed(self, tab: Conversation, event: AgentEvent) -> None:
        child = str(event.data.get("child_run_id", ""))
        status = str(event.data.get("status", "completed"))
        row = tab.subagent_rows.get(child)
        if row is not None:
            row.finish(status)
        self._refresh_sidebar()

    def _on_input_received(self, tab: Conversation, text: str) -> None:
        """A queued message reached the model: show it where it was read."""
        bubble = next((b for b in tab.queued if b.text == text), None)
        if bubble is None:
            return
        tab.queued.remove(bubble)
        bubble.remove()
        tab.mount(UserMessage(text))

    def _finish_run(self, tab: Conversation, outcome: str) -> None:
        if not tab.running and outcome == "failed":
            return
        if tab.queued:  # a stopped run never read them: hand them back to edit or resend
            text = "\n\n".join(b.text for b in tab.queued)
            if tab is self.tab:
                prompt = self.query_one("#prompt", PromptArea)
                prompt.load_text(f"{text}\n\n{prompt.text}".strip())
            else:
                tab.draft = f"{text}\n\n{tab.draft}".strip()
            for bubble in tab.queued:
                bubble.remove()
            tab.queued.clear()
        self._close_approval_dialogs(tab)
        tab.awaiting = 0
        tab.elapsed = time.monotonic() - tab.run_started if tab.run_started else 0.0
        tab.outcome = "cancelled" if tab.cancel_sent and outcome == "failed" else outcome
        tab.running = False
        tab.cancel_sent = False
        for card in tab.cards.values():
            if card.status == "running":
                card.finish("done")
            elif card.status == "waiting":  # the run ended while it was asking
                card.finish("failed")
        self._refresh_all()
        self._spawn(self._sync_usage(tab))
        if not tab.named:
            self._spawn(self._sync_title(tab))

    async def _finish_with_diff(
        self, tab: Conversation, card: Disclosure, call_id: str, event: AgentEvent
    ) -> None:
        """Fill a write/edit card with the diff of what that call changed, then settle it."""
        path = tab.file_paths.pop(call_id, "")
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
        if self.tab.session_id is None:
            self.notify("Nothing has been changed yet.", severity="information")
            return
        try:
            files = await self.application.changed_files(self.tab.session_id)
        except Exception as exc:
            self.notify(f"Could not read the changes: {exc}", severity="error")
            return
        self.push_screen(ChangesScreen(files))

    async def _sync_usage(self, tab: Conversation) -> None:
        """Replace the running total with the durable one (it also holds subagent runs)."""
        session_id = tab.session_id
        if session_id is None:
            return
        try:
            usage = await self.application.session_usage(session_id)
        except Exception:  # a cost display must never break the conversation
            return
        if tab.session_id == session_id:
            tab.usage = usage
            self._refresh_all()

    async def _sync_title(self, tab: Conversation, tries: int = 5) -> None:
        """Pick up the title the small model gave the conversation (it lands in the background)."""
        session_id = tab.session_id
        for attempt in range(tries):
            if session_id is None or tab.session_id != session_id:
                return
            with contextlib.suppress(Exception):  # a title is cosmetic
                session = await self.application.get_session(session_id)
                title = session.title if session is not None else ""
                if title and title != (tab.title or "")[:80]:
                    tab.title, tab.named = title, True
                    self._refresh_all()
                    return
            if attempt + 1 < tries:
                await asyncio.sleep(2)

    # ── conversation helpers ──────────────────────────────────────────────────
    def _remove_welcome(self, tab: Conversation) -> None:
        """First content ends the empty state and starts following the output.

        The chat is anchored only once there is a conversation: an anchored container
        bottom-aligns short content, which would push the welcome panel to the bottom.
        """
        for welcome in tab.query(Welcome):
            welcome.remove()
        if not tab.is_anchored:
            tab.anchor()  # follow new output unless the user scrolls up

    def _bump_tokens(self, tab: Conversation, text: str) -> None:
        """Approximate context usage until the model reports it: ~4 chars per token."""
        tab.tokens += max(1, len(text) // 4)
        self._refresh_sidebar()

    # ── tab bar / sidebar ─────────────────────────────────────────────────────
    def _tick(self) -> None:
        now = time.monotonic()
        for tab in self._tabs:
            for card in tab.cards.values():
                card.tick(now)
            for row in tab.subagent_rows.values():
                row.tick(now)
        if any(tab.running for tab in self._tabs):
            self._refresh_tabs(now)
        if self.tab.running:
            self._refresh_sidebar(now)

    def _refresh_all(self) -> None:
        if len(self.query(TabBar)) == 0:
            return  # fires in the gap between screen teardown and app unmount
        self._refresh_tabs()
        self._refresh_sidebar()
        self._refresh_prompt()

    def _refresh_tabs(self, now: float | None = None) -> None:
        bars = self.query(TabBar)
        if not bars:
            return
        now = time.monotonic() if now is None else now
        labels = []
        for tab in self._tabs:
            if tab.running:
                mark = "? " if tab.awaiting else f"{spinner_frame(now)} "
            else:
                mark = "✗ " if tab.outcome == "failed" else ""
            labels.append(mark + ellipsize(tab.title or "New conversation", _TAB_TITLE))
        bars.first(TabBar).show(labels, self._tabs.index(self.tab))

    def _status(self, now: float | None = None) -> str:
        """This tab's run state, as markup: working (with time), waiting, done, failed…"""
        tab = self.tab
        now = time.monotonic() if now is None else now
        if tab.running and tab.cancel_sent:
            return "[$warning]cancelling…[/]"
        if tab.running and tab.awaiting:
            return "[$warning]needs your approval[/]"
        if tab.running:
            return (
                f"[$accent]{spinner_frame(now)} working {format_duration(now - tab.run_started)}[/]"
            )
        if tab.outcome == "done":
            return f"[$success]✓ done in {format_duration(tab.elapsed)}[/]"
        if tab.outcome == "cancelled":
            return "[$warning]■ cancelled[/]"
        if tab.outcome == "failed":
            return "[$error]✗ failed[/]"
        return "[$text-muted]idle[/]"

    def _sidebar_state(self, now: float | None = None) -> SidebarState:
        tab = self.tab
        running = sum(1 for c in tab.cards.values() if c.status == "running")
        return SidebarState(
            title=tab.title or "New conversation",
            session=f"session {tab.session_id}" if tab.session_id else "not saved yet",
            tokens=tab.context or tab.tokens,
            estimated=not tab.context,
            window=self.application.config.context_window,
            tools_run=tab.tools_run,
            tools_running=running,
            subagents=[(r.agent_name, r.outcome) for r in tab.subagent_rows.values()],
            workspace=_short_path(self.application.config.workspace, 28),
            branch=self._branch,
            state=self._status(now),
            cost=usage_summary(tab.usage, context=False) if tab.usage.calls else "",
            mode=self.application.get_approval_mode(),
            model=self.application.get_model(),
        )

    def _refresh_sidebar(self, now: float | None = None) -> None:
        if len(self.query("#sidebar")) == 0:
            return
        self.query_one(Sidebar).render_state(self._sidebar_state(now))

    def _refresh_prompt(self) -> None:
        prompt = self.query_one("#prompt", PromptArea)
        prompt.placeholder = (
            "Working… type to queue a message (up arrow takes the last one back)"
            if self.tab.running
            else "Ask anything…"
        )

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

    async def action_rewind(self) -> None:
        """Pick an earlier message and go back to it (conversation, code, or both)."""
        if self._busy():
            return
        session_id = self.tab.session_id
        if session_id is None:
            self.notify("Nothing to rewind to: this conversation has no messages yet.")
            return
        try:
            turns = await self.application.timeline.lineage(session_id)
        except Exception as exc:  # a conversation with no event log, say
            self.notify(f"Cannot rewind here: {exc}", severity="error")
            return
        turns = [t for t in turns if t.turn.user_text]
        if not turns:
            self.notify(
                f"Nothing to rewind to: no earlier messages found for {session_id}.",
                severity="warning",
            )
            return
        self.push_screen(RewindScreen(turns), self._on_rewind_picked)

    async def action_restore_files(self) -> None:
        """Put the folder in this conversation's file state (after opening another one)."""
        if self._busy() or self.tab.session_id is None:
            return
        try:
            result = await self.application.timeline.restore_files(self.tab.session_id)
        except Exception as exc:
            self.notify(f"Could not restore files: {exc}", severity="error")
            return
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
        try:
            differ = await self.application.timeline.files_differ(session_id)
        except Exception:
            return
        if differ:
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
        current = self.tab.session_id
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
        if outcome.session_id != current:
            self._titles[outcome.session_id] = "rewound"
            await self._open_session(outcome.session_id, here=True)
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
        if key == "pageup":
            self.tab.scroll_page_up()
        else:
            self.tab.scroll_page_down()

    async def action_switch_session(self) -> None:
        """Open the conversation picker."""
        sessions = await self.application.list_sessions(self._principal)
        self._titles = {s.id: s.title for s in sessions}
        self.push_screen(
            SessionPicker(sessions, self.tab.session_id, self._delete_session),
            self._on_session_picked,
        )

    async def _delete_session(self, session_id: str) -> None:
        """Delete a stored conversation; a tab showing it closes (or empties, if it is the last)."""
        tab = next((t for t in self._tabs if t.session_id == session_id), None)
        if tab is not None and tab.running:
            self.notify("That conversation is running. Cancel it first.", severity="warning")
            return
        await self.application.delete_session(session_id)
        self._titles.pop(session_id, None)
        if tab is not None:
            if len(self._tabs) == 1:
                await self._replace_tab(tab)
            else:
                self._tabs.remove(tab)
                if tab is self.tab:
                    self._show_tab(self._tabs[0])
                await tab.remove()
                self._refresh_all()
        self.notify("Conversation deleted.")

    def _on_session_picked(self, session_id: str | None) -> None:
        if session_id is not None and session_id != self.tab.session_id:
            self._spawn(self._open_session(session_id))

    async def _open_session(self, session_id: str, *, here: bool = False) -> None:
        """Show a stored conversation: its tab if one is open, else a tab with its history.

        ``here`` loads it into this tab (a rewind's branch, the conversation to resume);
        otherwise an empty tab is reused and anything else stays open in its own tab.
        """
        existing = next((t for t in self._tabs if t.session_id == session_id), None)
        if existing is not None and not here:
            self._show_tab(existing)
            return
        history = await self.application.session_history(session_id)
        tab = await (self._replace_tab(self.tab) if here or self.tab.blank else self._add_tab())
        tab.session_id = session_id
        with contextlib.suppress(Exception):  # the cost is informational; history matters more
            tab.usage = await self.application.session_usage(session_id)
            tab.context = tab.usage.last_input_tokens
        title = self._titles.get(session_id, "")
        tab.title = None if title in ("", "Untitled") else title
        tab.named = tab.title is not None
        if history:
            self._remove_welcome(tab)
        for item in history:
            if item.kind == "user":
                tab.mount(UserMessage(item.text))
            elif item.kind == "assistant":
                tab.mount(AgentMessage(item.text))
            elif item.kind == "compaction":
                tab.mount(Disclosure("think", "context compacted", "summary above", body=item.text))
            elif item.kind == "tool":
                tab.mount(
                    Disclosure(
                        "tool",
                        item.text,
                        tool_detail(item.args),
                        body=tool_args_lines(item.args),
                    )
                )
            tab.tokens += max(1, len(item.text) // 4)
        self._refresh_all()
        self.call_after_refresh(tab.scroll_end, animate=False)
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
        if self.tab.last_child_id is not None:
            await self.application.cancel_run(self.tab.last_child_id)


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
