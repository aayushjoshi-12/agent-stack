"""The Textual TUI gateway.

A single-screen Textual app that drives the :class:`Application`: the conversation
streams messages, thinking and tool calls, the side panes show runs, subagents and
running shells, and the keyboard mirrors the opencode-style bindings. It touches
only ``Application``, ``gateways.base`` and ``core.events`` — every run goes through
``Application.send_message`` and every cancel through ``Application.cancel_run``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, ClassVar, cast

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import Hit, Hits, Provider
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.timer import Timer
from textual.widgets import (
    Button,
    Collapsible,
    Footer,
    Input,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
    Tree,
)

from agent_stack.app.application import Application, IdentityResolver, SessionResolver
from agent_stack.core.events import AgentEvent, EventType
from agent_stack.gateways.base import MessageEnvelope
from agent_stack.gateways.tui.render import (
    assistant_block,
    event_block,
    failure_block,
    human_block,
    reasoning_block,
    subagent_block,
    tool_output_text,
    tool_started_block,
    welcome_block,
)

__all__ = ["TuiApp", "TuiCommands"]

_SPINNER = "⠋⠹⠸⠼⠴⠦⠧⠇⠏"
#: The pane widgets Tab / Shift+Tab cycle between, in order.
_PANE_IDS = ("prompt", "subagents", "shells")
_QUIT_WORDS = frozenset({"exit", "quit"})


@dataclass
class ShellState:
    """A running/settled shell command shown in the Shells pane."""

    tool: str
    run_id: str
    output: str = ""
    status: str = "running"
    #: How much of ``output`` has already been written to the shell log (always ends
    #: on a line boundary, so a partial final line is not emitted twice).
    emitted: int = 0


class Divider(Static):
    """A draggable one-cell rule; drag it to resize the pane beside it.

    ``sign`` is +1 when dragging right widens ``target`` (the left sidebar) and -1
    when it narrows it (the right-hand panes).
    """

    MIN_WIDTH = 14
    MAX_WIDTH = 80
    ALLOW_SELECT = False

    def __init__(self, target: str, *, sign: int, id: str) -> None:
        super().__init__(id=id)
        self.target = target
        self.sign = sign
        self._start_x: int | None = None
        self._start_width = 0

    def on_mouse_down(self, event: events.MouseDown) -> None:
        event.stop()
        self.capture_mouse()
        self._start_x = event.screen_x
        self._start_width = self.app.query_one(f"#{self.target}").outer_size.width

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self._start_x is None:
            return
        width = self._start_width + self.sign * (event.screen_x - self._start_x)
        self.app.query_one(f"#{self.target}").styles.width = max(
            self.MIN_WIDTH, min(self.MAX_WIDTH, width)
        )

    def on_mouse_up(self, event: events.MouseUp) -> None:
        self._start_x = None
        self.release_mouse()


class TuiCommands(Provider):
    """App commands surfaced in the Ctrl+P command palette."""

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        app = cast("TuiApp", self.app)
        commands: list[tuple[str, Any]] = [
            ("Cancel current run", app.action_cancel_or_quit),
            ("Focus prompt", lambda: app.query_one("#prompt", Input).focus()),
            ("Clear chat", app.action_clear_chat),
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
    # (``$accent``, ``$secondary``, ``ansi_bright_black``) all resolve to the
    # terminal's palette; there is no hex/RGB anywhere. Secondary text uses
    # ``text-style: dim`` because ``$text-muted`` flattens under ANSI themes.
    # Spacing uses a fixed scale (0 · 1 · 2 cells) and regions are separated by
    # whitespace and a single draggable rule, never boxes. See ADR 011.
    CSS = """
    Screen { background: $background; color: $text; }

    /* scrollbars: one cell thick, transparent track, muted thumb */
    #chat, #runs, #subagents, #shells, #subagent-log, #shell-log {
        scrollbar-size-vertical: 1;
        scrollbar-background: transparent;
        scrollbar-color: ansi_bright_black;
        scrollbar-color-hover: $accent;
        scrollbar-color-active: $accent;
    }

    /* widget defaults: terminal-default colour, no boxes */
    Input { border: none; background: transparent; }
    RichLog { background: transparent; }
    Tree { background: transparent; }
    VerticalScroll { background: transparent; }

    /* Footer: keys are bold terminal-default text, descriptions recede with dim. */
    FooterKey .footer-key--key { color: ansi_default; text-style: bold; }
    FooterKey .footer-key--description { color: ansi_default; text-style: dim; }

    /* tabs: single row, no underline bar, the active tab is an accent */
    Tabs { height: 1; background: transparent; }
    Tab { padding: 0 2; color: ansi_default; text-style: dim; }
    Tab.-active { color: $accent; text-style: bold; }
    Underline { display: none; }

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

    /* header: identity left, secondary runtime metadata right */
    #header { height: 1; padding: 0 2; }
    #header-title { width: auto; color: $accent; text-style: bold; }
    #header-meta { width: 1fr; text-align: right; text-style: dim; }

    #body { height: 1fr; }

    /* three regions, separated by whitespace and a single draggable rule */
    #left { width: 26; padding: 1 2; }
    #center { width: 1fr; padding: 1 3; }
    #panes { width: 46; padding: 1 2; }

    Divider { width: 1; height: 1fr; background: ansi_bright_black; }
    Divider:hover { background: $accent; }

    .pane-title { height: 1; text-style: dim; margin-bottom: 1; }

    #runs, #chat, #subagents, #subagent-log, #shell-log { height: 1fr; }
    #shells { height: auto; max-height: 8; margin-bottom: 1; }
    #subagent-controls { height: 3; }
    #subagent-input { width: 1fr; }

    /* conversation: a stack of message widgets with breathing room */
    .welcome { width: 100%; margin: 1 0 2 0; }
    .msg { width: 100%; margin-top: 1; }
    .msg-user { background: ansi_bright_black; color: ansi_default; padding: 0 1; }
    .msg-think { border: none; padding: 0; background: transparent; }
    CollapsibleTitle {
        width: auto; padding: 0; background: transparent;
        color: ansi_default; text-style: dim;
    }
    CollapsibleTitle:hover { background: transparent; color: $accent; }
    CollapsibleTitle:focus { background: transparent; color: $accent; text-style: bold; }
    .think-body { color: ansi_default; text-style: dim italic; }
    .tool-line { width: 100%; margin-top: 1; }
    .subagent-line { width: 100%; }

    /* input: a rule separates it from history; no box, terminal-default colour */
    #prompt {
        height: 3;
        border: none;
        border-top: solid ansi_bright_black;
        padding: 1 3 0 3;
    }

    /* narrow terminals: drop the side panes, then the sidebar */
    Screen.narrow #panes, Screen.narrow #divider-panes { display: none; }
    Screen.tiny #left, Screen.tiny #divider-left { display: none; }
    """

    BINDINGS: ClassVar[list[Any]] = [
        Binding("ctrl+c", "cancel_or_quit", "cancel/quit", priority=True),
        Binding("ctrl+q", "quit", "quit"),
        Binding("tab", "cycle_panes(1)", "next pane", priority=True),
        Binding("shift+tab", "cycle_panes(-1)", "prev pane", priority=True),
        Binding("ctrl+b", "toggle_panes", "panes", priority=True),
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
        self._frame = 0
        self._welcome: Static | None = None

        self._run_rows: dict[str, Any] = {}
        self._shell_nodes: dict[str, Any] = {}
        self._selected_shell: str | None = None
        self.shells: dict[str, ShellState] = {}

        self._child_nodes: dict[str, Any] = {}
        self.child_events: dict[str, list[AgentEvent]] = {}
        self._selected_child: str | None = None

        self._tasks: set[asyncio.Task[None]] = set()
        self._spinner_timer: Timer | None = None
        self._force_panes = False

    # ── layout ────────────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        with Horizontal(id="header"):
            yield Static("agent-stack", id="header-title")
            yield Static("", id="header-meta")
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield Static("runs", classes="pane-title")
                yield Tree("runs", id="runs")
            yield Divider("left", sign=1, id="divider-left")
            with Vertical(id="center"):
                yield Static("conversation", classes="pane-title")
                yield VerticalScroll(id="chat")
            yield Divider("panes", sign=-1, id="divider-panes")
            with TabbedContent(id="panes"):
                with TabPane("subagents", id="tab-subagents"):
                    yield Tree("subagents", id="subagents")
                    yield RichLog(id="subagent-log", markup=False, wrap=True, highlight=False, min_width=0)
                    with Horizontal(id="subagent-controls"):
                        yield Button("interrupt", id="subagent-interrupt")
                        yield Input(placeholder="steer child…", id="subagent-input")
                with TabPane("shells", id="tab-shells"):
                    yield Tree("shells", id="shells")
                    yield RichLog(id="shell-log", markup=False, wrap=True, highlight=False, min_width=0)
                    yield Button("kill run", id="shell-kill")
        yield Input(
            placeholder="message   (ctrl+p palette · /agent <name> <task>)",
            id="prompt",
        )
        yield Footer()

    async def on_mount(self) -> None:
        for tree in ("#runs", "#subagents", "#shells"):
            widget = self.query_one(tree, Tree)
            # Flat, unguided lists: the root is redundant and Textual's guide
            # glyphs (├── ▼) read as noise, so show leaves at column zero.
            widget.show_root = False
            widget.show_guides = False
            widget.guide_depth = 2

        if self._session_id is None:
            resolver = SessionResolver(self.application.session_store, self.application.agent.name)
            self._session_id = await resolver.resolve(self._principal)

        self._welcome = Static(welcome_block(), classes="welcome")
        self.query_one("#chat", VerticalScroll).mount(self._welcome)

        self._apply_responsive()
        self.query_one("#prompt", Input).focus()
        self._update_header()
        self._spinner_timer = self.set_interval(1 / 10, self._tick_spinner)

    def on_resize(self, event: events.Resize) -> None:
        self._apply_responsive(event.size.width)

    def _apply_responsive(self, width: int | None = None) -> None:
        """Collapse secondary panes as the terminal narrows (no media queries in CSS).

        At narrow widths the right-hand panes are hidden and at tiny widths the session
        sidebar follows — but ``ctrl+b`` forces the panes back on, hiding the sidebar
        instead so the panes keep the room they need. Panes are therefore never
        permanently unreachable.
        """
        # Timers/resize can fire after the screen is torn down but before on_unmount;
        # guard on the screen stack so teardown does not raise.
        if not self.screen_stack:
            return
        if width is None:
            width = self.size.width
        self.screen.set_class(width < 100 and not self._force_panes, "narrow")
        self.screen.set_class(width < 68 or self._force_panes, "tiny")

    def on_unmount(self) -> None:
        if self._spinner_timer is not None:
            self._spinner_timer.stop()
        for task in list(self._tasks):
            task.cancel()

    # ── input ─────────────────────────────────────────────────────────────────
    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if event.input.id == "subagent-input":
            if text and self._selected_child is not None:
                await self.application.send_input(self._selected_child, text)
            return
        if not text:
            return
        if text in _QUIT_WORDS:
            self.exit()
            return
        await self._submit_prompt(text)

    async def _submit_prompt(self, text: str) -> None:
        if self._welcome is not None:
            self._welcome.remove()
            self._welcome = None
        self._write_message(human_block(text), "msg-user")
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
        self._track_run(handle.run_id, "running")
        self._update_header()
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
        """Route one runtime event to the right pane/status line."""
        if len(self.query("#chat")) == 0:
            # The screen has been torn down (quit or test teardown) while a stream
            # was still live; drop the event rather than touching dead widgets.
            return
        if event.type is EventType.SUBAGENT_STARTED:
            self._on_subagent_started(event)
        elif event.type is EventType.SUBAGENT_COMPLETED:
            self._on_subagent_completed(event)
        elif event.parent_run_id is not None and event.run_id != self._active_run_id:
            self._on_child_event(event)
        else:
            self._on_main_event(event)

    def _on_main_event(self, event: AgentEvent) -> None:
        etype = event.type
        if etype is EventType.RUN_STARTED:
            self._track_run(event.run_id, "running")
        elif etype is EventType.MODEL_REASONING:
            self._write_reasoning(event)
        elif etype is EventType.ASSISTANT_MESSAGE:
            self._write_message(assistant_block(event), "msg-agent")
        elif etype is EventType.TOOL_STARTED:
            self._write_message(tool_started_block(event), "tool-line")
            if str(event.data.get("tool")) == "run_command":
                self._ensure_shell(str(event.data.get("tool_call_id")), "run_command", event.run_id)
        elif etype is EventType.TOOL_OUTPUT:
            tool_call_id = str(event.data.get("tool_call_id"))
            self._ensure_shell(tool_call_id, str(event.data.get("tool", "command")), event.run_id)
            self._append_shell(tool_call_id, tool_output_text(event))
        elif etype is EventType.TOOL_COMPLETED:
            self._finish_shell(str(event.data.get("tool_call_id")))
        elif etype is EventType.RUN_FAILED:
            self._write_message(failure_block(event), "msg-error")
        elif etype is EventType.RUN_COMPLETED:
            pass  # a successful run is silent: no "run completed" line
        if etype in (EventType.RUN_COMPLETED, EventType.RUN_FAILED):
            self._run_active = False
            self._cancel_sent = False
            accepted = etype is EventType.RUN_COMPLETED
            self._track_run(event.run_id, "done" if accepted else "failed")
            self._update_header()

    def _on_child_event(self, event: AgentEvent) -> None:
        child = event.run_id
        self.child_events.setdefault(child, []).append(event)
        if child not in self._child_nodes:
            self._add_child_node(child, str(event.data.get("agent", "subagent")))
        if child == self._selected_child:
            self._subagent_log().write(event_block(event))

    def _on_subagent_started(self, event: AgentEvent) -> None:
        child = str(event.data.get("child_run_id", ""))
        if not child:
            return
        self._add_child_node(child, str(event.data.get("agent_name", child)))
        self._write_message(subagent_block(event, done=False), "subagent-line")
        self._spawn(self._consume_child(child))

    def _on_subagent_completed(self, event: AgentEvent) -> None:
        child = str(event.data.get("child_run_id", ""))
        node = self._child_nodes.get(child)
        if node is not None:
            name = str(event.data.get("agent_name", child))
            node.label = f"{name}  done"
        self._write_message(subagent_block(event, done=True), "subagent-line")

    async def _consume_child(self, child_id: str) -> None:
        try:
            async for event in self.application.subscribe(child_id):
                self.handle_event(event)
        except asyncio.CancelledError:
            raise
        except Exception:  # the child stream is best-effort; the tree still shows it
            pass

    def _add_child_node(self, child_id: str, name: str) -> None:
        if child_id in self._child_nodes:
            return
        tree = self.query_one("#subagents", Tree)
        self._child_nodes[child_id] = tree.root.add_leaf(name, data=child_id)

    def _select_child(self, child_id: str) -> None:
        self._selected_child = child_id
        log = self._subagent_log()
        log.clear()
        for event in self.child_events.get(child_id, []):
            log.write(event_block(event))

    # ── conversation ──────────────────────────────────────────────────────────
    def _chat(self) -> VerticalScroll:
        return self.query_one("#chat", VerticalScroll)

    def _write_message(self, text: Text, css_class: str) -> None:
        """Append a message row to the conversation, then keep it in view."""
        if not text.plain.strip():
            return
        self._chat().mount(Static(text, classes=f"msg {css_class}"))
        self.call_after_refresh(self._scroll_chat_end)

    def _write_reasoning(self, event: AgentEvent) -> None:
        body = reasoning_block(event)
        if not body.plain.strip():
            return
        self._chat().mount(
            Collapsible(
                Static(body, classes="think-body"),
                title="thinking",
                collapsed=True,
                collapsed_symbol="[+]",
                expanded_symbol="[-]",
                classes="msg msg-think",
            )
        )
        self.call_after_refresh(self._scroll_chat_end)

    def _scroll_chat_end(self) -> None:
        # Runs after the next layout; the chat may already be gone during teardown.
        chats = self.query("#chat")
        if len(chats):
            chats.first(VerticalScroll).scroll_end(animate=False)

    def action_clear_chat(self) -> None:
        self._chat().remove_children()
        self._welcome = None

    def _subagent_log(self) -> RichLog:
        return self.query_one("#subagent-log", RichLog)

    def _shell_log(self) -> RichLog:
        return self.query_one("#shell-log", RichLog)

    def _ensure_shell(self, tool_call_id: str, tool: str, run_id: str) -> None:
        if tool_call_id in self.shells:
            return
        self.shells[tool_call_id] = ShellState(tool=tool, run_id=run_id)
        tree = self.query_one("#shells", Tree)
        node = tree.root.add_leaf(f"{_short(tool, 18)}  running", data=tool_call_id)
        self._shell_nodes[tool_call_id] = node
        if self._selected_shell is None:
            self._select_shell(tool_call_id)

    def _append_shell(self, tool_call_id: str, chunk: str) -> None:
        state = self.shells.get(tool_call_id)
        if state is None:
            return
        state.output += chunk
        if tool_call_id == self._selected_shell:
            self._flush_shell(state, final=False)

    def _flush_shell(self, state: ShellState, *, final: bool) -> None:
        """Write new complete lines of ``state.output`` to the selected shell's log.

        Stream chunks are raw reads, not line-aligned, so a trailing partial line is
        held back until it is completed (or the shell finishes).
        """
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
        for line in text.split("\n"):
            self._shell_log().write(Text(line, style="dim"))
        state.emitted += consumed

    def _select_shell(self, tool_call_id: str) -> None:
        self._selected_shell = tool_call_id
        self._shell_log().clear()
        state = self.shells.get(tool_call_id)
        if state is None:
            return
        state.emitted = 0
        self._flush_shell(state, final=state.status != "running")

    def _finish_shell(self, tool_call_id: str) -> None:
        state = self.shells.get(tool_call_id)
        if state is None:
            return
        state.status = "done"
        node = self._shell_nodes.get(tool_call_id)
        if node is not None:
            node.label = f"{_short(state.tool, 18)}  done"
        if tool_call_id == self._selected_shell:
            self._flush_shell(state, final=True)
        self._update_header()

    def _track_run(self, run_id: str, status: str) -> None:
        tree = self.query_one("#runs", Tree)
        label = f"#{_short(run_id, 8)}  {status}"
        node = self._run_rows.get(run_id)
        if node is not None:
            node.label = label
            return
        self._run_rows[run_id] = tree.root.add_leaf(label, data=run_id)

    # ── actions / status ──────────────────────────────────────────────────────
    async def action_cancel_or_quit(self) -> None:
        if self._active_run_id is not None and self._run_active and not self._cancel_sent:
            self._cancel_sent = True
            await self.application.cancel_run(self._active_run_id)
        else:
            self.exit()

    def action_cycle_panes(self, delta: int) -> None:
        widgets = [self.query_one(f"#{pane_id}") for pane_id in _PANE_IDS]
        focused = self.focused
        if focused is not None and focused.id in _PANE_IDS:
            index = (_PANE_IDS.index(focused.id) + delta) % len(_PANE_IDS)
        else:
            index = 0
        widgets[index].focus()

    def action_toggle_panes(self) -> None:
        """Force the side panes on (or back to the width-driven default)."""
        self._force_panes = not self._force_panes
        self._apply_responsive()

    def _tick_spinner(self) -> None:
        if not self._run_active:
            return
        self._frame = (self._frame + 1) % len(_SPINNER)
        self._update_header()

    def _update_header(self) -> None:
        metas = self.query("#header-meta")
        if len(metas) == 0:
            # Fires in the gap between screen teardown and app unmount.
            return
        state = f"{_SPINNER[self._frame]} running" if self._run_active else "idle"
        session = _short(self._session_id or "…", 24)
        metas.first(Static).update(
            f"{session}  ·  {self.application.config.model}  ·  {state}"
        )

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "subagent-interrupt":
            if self._selected_child is not None:
                await self.application.cancel_run(self._selected_child)
        elif event.button.id == "shell-kill":
            state = self.shells.get(self._selected_shell or "")
            if state is not None:
                await self.application.cancel_run(state.run_id)
            elif self._active_run_id is not None:
                await self.application.cancel_run(self._active_run_id)

    def on_tree_node_selected(self, event: Tree.NodeSelected[Any]) -> None:
        data = event.node.data
        if not isinstance(data, str):
            return
        if event.control.id == "subagents":
            self._select_child(data)
        elif event.control.id == "shells":
            self._select_shell(data)


def _short(text: str, width: int) -> str:
    """Truncate `text` to `width` columns with a trailing ellipsis."""
    return text if len(text) <= width else text[: width - 1] + "…"


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
