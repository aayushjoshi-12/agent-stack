"""The Textual TUI gateway.

A single-screen Textual app that drives the :class:`Application`: the conversation
streams messages, thinking, tool calls and terminal-transcript panels, a context
sidebar shows the session title and an estimated context usage, and the keyboard
mirrors the opencode-style bindings (Ctrl+P palette, Ctrl+C cancel/quit). It touches
only ``Application``, ``gateways.base`` and ``core.events`` — every run goes through
``Application.send_message`` and every cancel through ``Application.cancel_run``.

Layout follows the reference coding-agent UI: a wide conversation column on the left
(≈2fr) and a narrow context sidebar on the right (≈1fr). Sections are separated by
*panel colours* — the sidebar, the input composer and message panels are all a shade
darker than the conversation background — never by box-drawing rules (ADR 011).
Assistant messages render as Markdown, so fenced code blocks get the terminal-native
syntax highlighting Textual applies to code fences (``MarkdownFence``), and streamed
``run_command`` output is diff-coloured when it looks like unified-diff text.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, cast

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import Hit, Hits, Provider
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Collapsible, Input, Markdown, Static

from agent_stack.app.application import Application, IdentityResolver, SessionResolver
from agent_stack.core.events import AgentEvent, EventType
from agent_stack.gateways.base import MessageEnvelope
from agent_stack.gateways.tui.render import (
    failure_block,
    human_block,
    output_line_text,
    reasoning_block,
    subagent_block,
    tool_output_text,
    tool_started_block,
)

__all__ = ["TuiApp", "TuiCommands"]

#: Sensors that mark an output stream as unified-diff text (matches any prefix).
_DIFF_SENSORS = ("diff --git", "Index: ", "@@ ", "+++ ", "--- ")
#: Widgets Tab / Shift+Tab cycle between, in order.
_FOCUS_IDS = ("prompt", "chat")
_QUIT_WORDS = frozenset({"exit", "quit"})


@dataclass
class ShellState:
    """A running/settled shell command rendered as an inline transcript panel."""

    tool: str
    run_id: str
    block: Vertical | None = None
    output: str = ""
    status: str = "running"
    diff: bool = False
    #: How much of ``output`` has already been written to the panel (always ends on a
    #: line boundary, so a partial final line is not emitted twice).
    emitted: int = 0


class TuiCommands(Provider):
    """App commands surfaced in the Ctrl+P command palette."""

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        app = cast("TuiApp", self.app)
        commands: list[tuple[str, Any]] = [
            ("Cancel current run", app.action_cancel_or_quit),
            ("New conversation", app.action_clear_chat),
            ("Interrupt last subagent", app.action_interrupt_subagent),
            ("Focus prompt", lambda: app.query_one("#prompt", Input).focus()),
            ("Quit agent-stack", app.exit),
        ]
        for name, callback in commands:
            score = matcher.match(name)
            if score > 0:
                yield Hit(score, matcher.highlight(name), callback, text=name)


class TuiApp(App[None]):
    """The first real gateway: a Textual terminal UI over the Application."""

    TITLE = "agent-stack"

    # ── design system ─────────────────────────────────────────────────────────
    # Colours come from the *active theme*, never from literals here, so the app
    # inherits the terminal's own palette (light or dark). The ANSI accents
    # (``$accent``, ``ansi_magenta``, ``ansi_bright_black``…) resolve to the
    # terminal's palette; there is no hex/RGB anywhere. Secondary text uses
    # ``text-style: dim`` because ``$text-muted`` flattens under ANSI themes.
    # Regions are separated by *darker panels* (the ``.panel`` class), never by
    # box-drawing rules: sidebar, composer, user messages, command transcripts and
    # code blocks all sit on a slightly darker background than the conversation.
    # ``:dark``/``:light`` flip the panel shade so light terminals stay readable.
    # See ADR 011.
    CSS = """
    Screen { background: $background; color: $text; }

    /* the one scrollbar: one cell thick, transparent track, muted thumb */
    #chat {
        scrollbar-size-vertical: 1;
        scrollbar-background: transparent;
        scrollbar-color: ansi_bright_black;
        scrollbar-color-hover: $accent;
        scrollbar-color-active: $accent;
    }

    /* widget defaults: terminal-default colour, no boxes */
    Input { border: none; background: transparent; color: ansi_default; }
    VerticalScroll { background: transparent; }

    /* darker panels replace divider lines (flip to light grey on light themes) */
    .panel { background: ansi_black; }
    .panel:light { background: ansi_bright_black; }

    #frame { height: 1fr; }

    /* conversation title row: bold task title + a small new-conversation control */
    #conv-title { height: 1; padding: 0 2; }
    #title-text { width: auto; color: ansi_default; text-style: bold; }
    #new-session { margin-left: 1; }

    #main { height: 1fr; }

    /* conversation column: chat, composer, status — the sidebar sits beside it */
    #conversation { width: 2fr; height: 1fr; }
    #chat { height: 1fr; padding: 0 2; }

    /* conversation rows, separated by whitespace (panels, not rules) */
    .row { width: 100%; margin-bottom: 1; }
    .msg-user { padding: 0 1; color: ansi_default; }
    .msg-agent { margin: 1 0; }
    .msg-error { color: ansi_default; }
    .tool-line, .subagent-line { color: ansi_default; }

    /* thinking: a collapsible ``Thought`` row, yellow label, dim-italic body.
       The Collapsible's own ``hkey`` border-top is a divider rule, so it is
       removed — the row separates by whitespace like every other section. */
    .msg-think { margin: 0 0 1 0; }
    Collapsible { border-top: none; background: transparent; }
    Collapsible:ansi { border-top: none; background: $panel; }
    CollapsibleTitle {
        width: auto; padding: 0; background: transparent;
        color: ansi_yellow; text-style: bold;
    }
    CollapsibleTitle:hover { background: transparent; color: $accent; }
    CollapsibleTitle:focus { background: transparent; color: $accent; }
    .think-body { color: ansi_default; text-style: dim italic; }

    /* terminal transcripts: an inline darker panel for bash commands */
    .cmd-block { padding: 0 1; }
    .cmd-block Static { width: 100%; }

    /* Markdown: assistant messages render natively, so headings are the accent
       and code fences get Textual's ANSI-aware syntax highlighting. */
    Markdown { height: auto; margin: 0; padding: 0; background: transparent; }
    MarkdownHeading,
    MarkdownH1, MarkdownH2, MarkdownH3, MarkdownH4, MarkdownH5, MarkdownH6 {
        background: transparent;
        color: ansi_magenta;
        text-style: bold;
        margin: 1 0 0 0;
    }
    MarkdownParagraph { margin: 0; }
    MarkdownBlock > .code_inline {
        background: ansi_black !important;
        color: ansi_default !important;
    }
    MarkdownBlock:light > .code_inline {
        background: ansi_bright_black !important;
        color: ansi_default !important;
    }
    MarkdownFence {
        background: ansi_black !important;
        color: ansi_default;
        margin: 1 0;
        padding: 0 1;
    }
    MarkdownFence:light { background: ansi_bright_black !important; }

    /* composer: a darker panel with the thin input accent on its left edge */
    #composer { height: 3; }
    #accent { width: 1; background: ansi_magenta; }
    #accent:light { background: ansi_magenta; }
    #composer-input { width: 1fr; padding: 0 2; }
    #prompt { height: 1; padding: 0; }
    #model-line { height: 1; color: ansi_bright_black; text-style: dim; }

    /* status bar: muted items, context in the accent */
    #status { height: 1; padding: 0 2; }
    #status-path, #status-file, #status-keys { color: ansi_bright_black; }
    #status-path, #status-file, #status-ctx { width: auto; margin-right: 2; }
    #status-ctx { color: $accent; }
    #status-keys { width: 1fr; text-align: right; }

    /* context sidebar: a darker panel, title + context usage, spacer, status */
    #sidebar { width: 1fr; height: 1fr; padding: 1 2; }
    #side-title { color: ansi_bright_white; text-style: bold; margin-bottom: 1; }
    #side-title:light { color: ansi_black; }
    #side-context { text-style: bold; margin: 2 0 0 0; }
    #side-tokens, #side-used, #side-status { color: ansi_bright_black; }
    #side-spacer { height: 1fr; }

    /* flat text controls (Textual's ANSI buttons default to a tall box) */
    Button:ansi.-style-flat, Button:ansi.-style-default {
        height: 1;
        min-width: 0;
        padding: 0 1;
        border: none;
        background: ansi_default;
        color: ansi_default;
        text-style: dim;
        &:hover { border: none; background: ansi_default; text-style: bold; }
        &.-active { border: none; background: ansi_default; }
    }
    #new-session { color: $accent; text-style: bold; }

    /* narrow terminals: drop the context sidebar, keeping the conversation */
    Screen.narrow #sidebar { display: none; }
    """

    BINDINGS: ClassVar[list[Any]] = [
        Binding("ctrl+c", "cancel_or_quit", "cancel/quit", priority=True),
        Binding("ctrl+q", "quit", "quit"),
        Binding("tab", "cycle_focus(1)", "next", priority=True),
        Binding("shift+tab", "cycle_focus(-1)", "prev", priority=True),
        Binding("ctrl+b", "toggle_sidebar", "sidebar", priority=True),
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

        theme = application.config.theme
        if theme in self.available_themes:
            self.theme = theme

        self._active_run_id: str | None = None
        self._run_active = False
        self._cancel_sent = False
        self._task_title: str | None = None
        self._tokens = 0
        self._branch = ""
        self._force_sidebar = False
        self._last_child_id: str | None = None

        self.shells: dict[str, ShellState] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    # ── layout ────────────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        with Vertical(id="frame"):
            with Horizontal(id="conv-title"):
                yield Static("agent-stack", id="title-text")
                yield Button("+", id="new-session")
            with Horizontal(id="main"):
                with Vertical(id="conversation"):
                    yield VerticalScroll(id="chat")
                    with Horizontal(id="composer"):
                        yield Static("", id="accent")
                        with Vertical(id="composer-input"):
                            yield Input(
                                placeholder="message   (ctrl+p commands)",
                                id="prompt",
                            )
                            yield Static("", id="model-line")
                    with Horizontal(id="status"):
                        yield Static("", id="status-path")
                        yield Static("", id="status-file")
                        yield Static("", id="status-ctx")
                        yield Static("ctrl+p commands", id="status-keys")
                with Vertical(id="sidebar"):
                    yield Static("agent-stack", id="side-title")
                    yield Static("Context", id="side-context")
                    yield Static("", id="side-tokens")
                    yield Static("", id="side-used")
                    yield Static("", id="side-spacer")
                    yield Static("", id="side-status")

    async def on_mount(self) -> None:
        if self._session_id is None:
            resolver = SessionResolver(self.application.session_store, self.application.agent.name)
            self._session_id = await resolver.resolve(self._principal)

        self.query_one("#model-line", Static).update(
            f"Build · {self.application.config.model}"
        )
        self._apply_responsive()
        self.query_one("#prompt", Input).focus()
        self._update_title()
        self._update_status()
        self._spawn(self._load_branch())

    def on_resize(self, event: events.Resize) -> None:
        self._apply_responsive(event.size.width)

    def _apply_responsive(self, width: int | None = None) -> None:
        """Collapse the context sidebar as the terminal narrows (no media queries).

        ``ctrl+b`` forces the sidebar back on, overriding the width heuristic.
        """
        # Resize can fire after the screen is torn down; guard so teardown cannot raise.
        if not self.screen_stack:
            return
        if width is None:
            width = self.size.width
        self.screen.set_class(width < 100 and not self._force_sidebar, "narrow")

    async def _load_branch(self) -> None:
        """Best-effort ``git branch --show-current`` for the status bar path."""
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
        if branch and len(self.query("#status-path")):
            self._branch = branch
            self._update_status()

    def on_unmount(self) -> None:
        for task in list(self._tasks):
            task.cancel()

    # ── input ─────────────────────────────────────────────────────────────────
    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        if text in _QUIT_WORDS:
            self.exit()
            return
        await self._submit_prompt(text)

    async def _submit_prompt(self, text: str) -> None:
        if self._task_title is None:
            self._task_title = text
            self._update_title()
        self._bump_tokens(text)
        self._write_line(human_block(text), "msg-user panel row")
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
        self._update_status()
        self._spawn(self._consume(handle))

    async def _consume(self, handle: Any) -> None:
        try:
            async for event in handle.events():
                self.handle_event(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a broken run must not tear down the UI
            self.notify(str(exc), severity="error")

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ── event routing ─────────────────────────────────────────────────────────
    def handle_event(self, event: AgentEvent) -> None:
        """Route one runtime event to the conversation/status line."""
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
            self._update_status()
        elif etype is EventType.MODEL_REASONING:
            self._write_reasoning(event)
        elif etype is EventType.ASSISTANT_MESSAGE:
            self._write_assistant(event)
        elif etype is EventType.TOOL_STARTED:
            self._on_tool_started(event)
        elif etype is EventType.TOOL_OUTPUT:
            self._on_tool_output(event)
        elif etype is EventType.TOOL_COMPLETED:
            state = self.shells.get(str(event.data.get("tool_call_id")))
            if state is not None:
                state.status = "done"
                self._flush_block(state, final=True)
        elif etype is EventType.RUN_FAILED:
            self._write_line(failure_block(event), "msg-error row")
        elif etype is EventType.RUN_COMPLETED:
            pass  # a successful run is silent: no "run completed" line
        if etype in (EventType.RUN_COMPLETED, EventType.RUN_FAILED):
            self._run_active = False
            self._cancel_sent = False
            self._update_status()

    def _on_subagent_started(self, event: AgentEvent) -> None:
        child = str(event.data.get("child_run_id", ""))
        if child:
            self._last_child_id = child
        self._write_line(subagent_block(event, done=False), "subagent-line row")

    def _on_subagent_completed(self, event: AgentEvent) -> None:
        self._write_line(subagent_block(event, done=True), "subagent-line row")

    # ── conversation ──────────────────────────────────────────────────────────
    def _chat(self) -> VerticalScroll:
        return self.query_one("#chat", VerticalScroll)

    def _write_line(self, text: Text, css_class: str) -> None:
        """Append a message row to the conversation, then keep it in view."""
        if not text.plain.strip():
            return
        self._chat().mount(Static(text, classes=css_class))
        self.call_after_refresh(self._scroll_chat_end)

    def _write_assistant(self, event: AgentEvent) -> None:
        content = str(event.data.get("content", ""))
        if not content.strip():
            return
        self._bump_tokens(content)
        self._chat().mount(Markdown(content, classes="msg-agent row"))
        self.call_after_refresh(self._scroll_chat_end)

    def _write_reasoning(self, event: AgentEvent) -> None:
        body = reasoning_block(event)
        if not body.plain.strip():
            return
        self._bump_tokens(body.plain)
        self._chat().mount(
            Collapsible(
                Static(body, classes="think-body"),
                title="Thinking",
                collapsed=True,
                collapsed_symbol="[+]",
                expanded_symbol="[-]",
                classes="msg-think",
            )
        )
        self.call_after_refresh(self._scroll_chat_end)

    def _on_tool_started(self, event: AgentEvent) -> None:
        tool = str(event.data.get("tool", "tool"))
        tool_call_id = str(event.data.get("tool_call_id", ""))
        if tool == "run_command" and tool_call_id:
            block = Vertical(classes="cmd-block panel row")
            self._chat().mount(block)
            # Mount the header row only once the block is attached, so it stays
            # the first child ahead of the streaming output rows (Textual defers
            # ``__init__`` children until the widget mounts, which would invert it).
            block.mount(Static(tool_started_block(event)))
            self.shells[tool_call_id] = ShellState(
                tool=tool, run_id=event.run_id, block=block
            )
        else:
            self._write_line(tool_started_block(event), "tool-line row")
        self.call_after_refresh(self._scroll_chat_end)

    def _on_tool_output(self, event: AgentEvent) -> None:
        state = self.shells.get(str(event.data.get("tool_call_id")))
        if state is None:
            return
        state.output += tool_output_text(event)
        self._flush_block(state)

    def _flush_block(self, state: ShellState, *, final: bool = False) -> None:
        """Write new complete lines of ``state.output`` into the transcript panel.

        Stream chunks are raw reads, not line-aligned, so a trailing partial line is
        held back until it is completed (or the tool finishes). Once the stream shows
        unified-diff markers the lines are diff-coloured (see ``output_line_text``).
        """
        if state.block is None:
            return
        fresh = state.output[state.emitted :]
        if not fresh:
            return
        if final:
            text: str = fresh.rstrip("\n")
            consumed = len(fresh)
        else:
            cut = fresh.rfind("\n")
            if cut == -1:
                return
            text = fresh[:cut]
            consumed = cut + 1
        if not state.diff:
            states = [st for st in text.split("\n") if st.startswith(_DIFF_SENSORS)]
            state.diff = bool(states)
        for line in text.split("\n"):
            state.block.mount(Static(output_line_text(line, diff=state.diff)))
        state.emitted += consumed
        self.call_after_refresh(self._scroll_chat_end)

    def _scroll_chat_end(self) -> None:
        # Runs after the next layout; the chat may already be gone during teardown.
        chats = self.query("#chat")
        if len(chats):
            chats.first(VerticalScroll).scroll_end(animate=False)

    def _bump_tokens(self, text: str) -> None:
        """Approximate context usage: ~4 chars per token, capped by the window."""
        self._tokens += max(1, len(text) // 4)
        self._update_status()

    def _ctx_percent(self) -> int:
        return min(100, round(self._tokens / 200_000 * 100))

    # ── title / status / sidebar ─────────────────────────────────────────────
    def _update_title(self) -> None:
        if len(self.query("#title-text")) == 0:
            return
        title = self._task_title or "agent-stack"
        self.query_one("#title-text", Static).update(_short(title, 36))
        self.query_one("#side-title", Static).update(_short(title, 60))

    def _update_status(self) -> None:
        if len(self.query("#status-ctx")) == 0:
            # Fires in the gap between screen teardown and app unmount.
            return
        path = _short_path(self.application.config.workspace, 26)
        self.query_one("#status-path", Static).update(
            f"{path}:{self._branch}" if self._branch else path
        )
        self.query_one("#side-status", Static).update(
            _short_path(self.application.config.workspace, 24)
        )
        if self._run_active and self._active_run_id is not None:
            run = f"run #{_short(self._active_run_id, 8)}"
        else:
            run = _short(f"session {self._session_id or ''}", 22)
        self.query_one("#status-file", Static).update(run)
        self.query_one("#status-ctx", Static).update(
            f"{self._tokens / 1000:.1f}K ({self._ctx_percent()}%)"
        )
        self.query_one("#side-tokens", Static).update(f"{self._tokens:,} tokens")
        self.query_one("#side-used", Static).update(f"{self._ctx_percent()}% used")

    # ── actions ──────────────────────────────────────────────────────────────
    async def action_cancel_or_quit(self) -> None:
        if self._active_run_id is not None and self._run_active and not self._cancel_sent:
            self._cancel_sent = True
            await self.application.cancel_run(self._active_run_id)
        else:
            self.exit()

    def action_clear_chat(self) -> None:
        """New conversation: drop history, reset the task title and context count."""
        self._chat().remove_children()
        self.shells.clear()
        self._task_title = None
        self._tokens = 0
        self._update_title()
        self._update_status()

    def action_cycle_focus(self, delta: int) -> None:
        widgets = [self.query_one(f"#{pane_id}") for pane_id in _FOCUS_IDS]
        focused = self.focused
        if focused is not None and focused.id in _FOCUS_IDS:
            index = (_FOCUS_IDS.index(focused.id) + delta) % len(_FOCUS_IDS)
        else:
            index = 0
        widgets[index].focus()

    def action_toggle_sidebar(self) -> None:
        """Force the context sidebar on (or back to the width-driven default)."""
        self._force_sidebar = not self._force_sidebar
        self._apply_responsive()

    async def action_interrupt_subagent(self) -> None:
        if self._last_child_id is not None:
            await self.application.cancel_run(self._last_child_id)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "new-session":
            self.action_clear_chat()


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