"""The Textual TUI gateway.

A single-screen Textual app that drives the :class:`Application`: the chat log
streams assistant/tool activity, the right-hand tabs show subagents, running shells,
and the audit tail, and the keyboard mirrors the opencode-style bindings. It touches
only ``Application``, ``gateways.base`` and ``core.events`` — every run goes through
``Application.send_message`` and every cancel through ``Application.cancel_run``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, cast

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import Hit, Hits, Provider
from textual.containers import Horizontal, Vertical
from textual.timer import Timer
from textual.widgets import (
    Button,
    DataTable,
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
    audit_line,
    event_block,
    header_status,
    human_block,
    run_status_block,
    subagent_block,
    tool_completed_block,
    tool_output_text,
    tool_started_block,
)

__all__ = ["TuiApp", "TuiCommands"]

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
#: The pane widgets Tab / Shift+Tab cycle between, in order.
_PANE_IDS = ("prompt", "subagents", "shells", "audit")
_QUIT_WORDS = frozenset({"exit", "quit"})


@dataclass
class ShellState:
    """A running/settled shell command shown in the Shells pane."""

    tool: str
    run_id: str
    output: str = ""
    status: str = "running"


class TuiCommands(Provider):
    """App commands surfaced in the Ctrl+P command palette."""

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        app = cast("TuiApp", self.app)
        commands: list[tuple[str, Any]] = [
            ("Cancel current run", app.action_cancel_or_quit),
            ("Focus prompt", lambda: app.query_one("#prompt", Input).focus()),
            ("Clear chat", lambda: app.query_one("#chat", RichLog).clear()),
            ("Quit agent-stack", app.exit),
        ]
        for name, callback in commands:
            score = matcher.match(name)
            if score > 0:
                yield Hit(score, matcher.highlight(name), callback, text=name)


class TuiApp(App[None]):
    """The first real gateway: a Textual terminal UI over the Application."""

    CSS = """
    #header { height: 1; background: $boost; color: $text; padding: 0 1; }
    #body { height: 1fr; }
    #left { width: 30; border: round $panel; }
    #center { width: 1fr; border: round $panel; }
    #panes { width: 48; border: round $panel; }
    .pane-title { background: $boost; color: $text-muted; padding: 0 1; }
    #runs { height: 1fr; }
    #chat { height: 1fr; }
    #subagents { height: 1fr; }
    #subagent-log { height: 1fr; }
    #subagent-controls { height: 3; }
    #subagent-input { width: 1fr; }
    #shells { height: 1fr; }
    #shell-kill { dock: bottom; }
    #audit { height: 1fr; }
    #prompt { height: 3; }
    """

    BINDINGS: ClassVar[list[Any]] = [
        Binding("ctrl+c", "cancel_or_quit", "Cancel / Quit", priority=True),
        Binding("ctrl+q", "quit", "Quit"),
        Binding("tab", "cycle_panes(1)", "Next pane", priority=True),
        Binding("shift+tab", "cycle_panes(-1)", "Prev pane", priority=True),
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

        self._active_run_id: str | None = None
        self._run_active = False
        self._cancel_sent = False
        self._frame = 0

        self._run_rows: dict[str, Any] = {}
        self._run_status_col: Any = None
        self._shell_cols: list[Any] = []
        self._shell_rows: dict[str, Any] = {}
        self._shell_by_row: dict[Any, str] = {}
        self._selected_shell: str | None = None
        self.shells: dict[str, ShellState] = {}

        self._child_nodes: dict[str, Any] = {}
        self.child_events: dict[str, list[AgentEvent]] = {}
        self._selected_child: str | None = None

        self._audit_offset = 0
        self._tasks: set[asyncio.Task[None]] = set()
        self._spinner_timer: Timer | None = None
        self._audit_timer: Timer | None = None

    # ── layout ────────────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        yield Static("agent-stack", id="header")
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield Static("Session / Runs", classes="pane-title")
                yield DataTable(id="runs")
            with Vertical(id="center"):
                yield Static("Chat", classes="pane-title")
                yield RichLog(id="chat", markup=False, wrap=True, highlight=False)
            with TabbedContent(id="panes"):
                with TabPane("Subagents", id="tab-subagents"):
                    yield Tree("subagents", id="subagents")
                    yield RichLog(id="subagent-log", markup=False, wrap=True, highlight=False)
                    with Horizontal(id="subagent-controls"):
                        yield Button("interrupt", id="subagent-interrupt")
                        yield Input(placeholder="steer child…", id="subagent-input")
                with TabPane("Shells", id="tab-shells"):
                    yield DataTable(id="shells")
                    yield Button("kill run", id="shell-kill")
                with TabPane("Audit", id="tab-audit"):
                    yield RichLog(id="audit", markup=False, wrap=True, highlight=False)
        yield Input(placeholder="> message   (Ctrl+P palette, /agent <name> <task>)", id="prompt")
        yield Footer()

    async def on_mount(self) -> None:
        runs = self.query_one("#runs", DataTable)
        runs.add_column("session")
        runs.add_column("run")
        self._run_status_col = runs.add_column("status")
        self._shell_cols = list(
            self.query_one("#shells", DataTable).add_columns("tool", "status", "out")
        )

        if self._session_id is None:
            resolver = SessionResolver(self.application.session_store, self.application.agent.name)
            self._session_id = await resolver.resolve(self._principal)

        self.query_one("#prompt", Input).focus()
        self._update_header()
        self._spinner_timer = self.set_interval(1 / 10, self._tick_spinner)
        self._audit_timer = self.set_interval(1 / 2, self.poll_audit)

    def on_unmount(self) -> None:
        for timer in (self._spinner_timer, self._audit_timer):
            if timer is not None:
                timer.stop()
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
        self._chat_write(human_block(text))
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
        elif etype is EventType.ASSISTANT_MESSAGE:
            self._chat_write(assistant_block(event))
        elif etype is EventType.TOOL_STARTED:
            self._chat_write(tool_started_block(event))
            if str(event.data.get("tool")) == "run_command":
                self._ensure_shell(str(event.data.get("tool_call_id")), "run_command", event.run_id)
        elif etype is EventType.TOOL_OUTPUT:
            tool_call_id = str(event.data.get("tool_call_id"))
            self._ensure_shell(tool_call_id, str(event.data.get("tool", "command")), event.run_id)
            self._append_shell(tool_call_id, tool_output_text(event))
        elif etype is EventType.TOOL_COMPLETED:
            self._chat_write(tool_completed_block(event))
            self._finish_shell(str(event.data.get("tool_call_id")))
        elif etype in (EventType.RUN_COMPLETED, EventType.RUN_FAILED):
            self._chat_write(run_status_block(event))
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
        self._chat_write(subagent_block(event, done=False))
        self._spawn(self._consume_child(child))

    def _on_subagent_completed(self, event: AgentEvent) -> None:
        child = str(event.data.get("child_run_id", ""))
        node = self._child_nodes.get(child)
        if node is not None:
            name = str(event.data.get("agent_name", child))
            node.label = f"{name} ✓ {event.data.get('status', 'done')}"
        self._chat_write(subagent_block(event, done=True))

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
        self._child_nodes[child_id] = tree.root.add(name, data=child_id, expand=True)

    def _select_child(self, child_id: str) -> None:
        self._selected_child = child_id
        log = self._subagent_log()
        log.clear()
        for event in self.child_events.get(child_id, []):
            log.write(event_block(event))

    # ── panes ─────────────────────────────────────────────────────────────────
    def _chat_write(self, renderable: Text) -> None:
        self.query_one("#chat", RichLog).write(renderable)

    def _subagent_log(self) -> RichLog:
        return self.query_one("#subagent-log", RichLog)

    def _ensure_shell(self, tool_call_id: str, tool: str, run_id: str) -> None:
        if tool_call_id in self.shells:
            return
        self.shells[tool_call_id] = ShellState(tool=tool, run_id=run_id)
        table = self.query_one("#shells", DataTable)
        row = table.add_row(tool, "running", "")
        self._shell_rows[tool_call_id] = row
        self._shell_by_row[row] = tool_call_id

    def _append_shell(self, tool_call_id: str, chunk: str) -> None:
        state = self.shells.get(tool_call_id)
        if state is None:
            return
        state.output += chunk
        table = self.query_one("#shells", DataTable)
        table.update_cell(self._shell_rows[tool_call_id], self._shell_cols[2], state.output)

    def _finish_shell(self, tool_call_id: str) -> None:
        state = self.shells.get(tool_call_id)
        if state is None:
            return
        state.status = "done"
        table = self.query_one("#shells", DataTable)
        table.update_cell(self._shell_rows[tool_call_id], self._shell_cols[1], "done")
        self._update_header()

    def _track_run(self, run_id: str, status: str) -> None:
        runs = self.query_one("#runs", DataTable)
        if run_id in self._run_rows:
            runs.update_cell(self._run_rows[run_id], self._run_status_col, status)
            return
        self._run_rows[run_id] = runs.add_row(self._session_id or "-", run_id[:8], status)

    # ── audit ─────────────────────────────────────────────────────────────────
    def _audit_path(self) -> Path:
        return Path(self.application.config.workspace) / ".agent" / "logs" / "audit.jsonl"

    def poll_audit(self) -> None:
        """Read new audit lines and append those for the active run (non-blocking-ish)."""
        path = self._audit_path()
        if not path.exists():
            return
        try:
            data = path.read_bytes()
        except OSError:
            return
        fresh = data[self._audit_offset :]
        self._audit_offset = len(data)
        log = self.query_one("#audit", RichLog)
        for raw in fresh.splitlines():
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if self._active_run_id is not None and record.get("run_id") != self._active_run_id:
                continue
            log.write(audit_line(record))

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

    def _tick_spinner(self) -> None:
        if not self._run_active:
            return
        self._frame = (self._frame + 1) % len(_SPINNER)
        self._update_header()

    def _update_header(self) -> None:
        tools = sum(1 for state in self.shells.values() if state.status == "running")
        self.query_one("#header", Static).update(
            header_status(
                agent=self.application.agent.name,
                session=self._session_id or "…",
                model=self.application.config.model,
                running=self._run_active,
                frame=_SPINNER[self._frame],
                tools=tools,
            )
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
        if isinstance(event.node.data, str):
            self._select_child(event.node.data)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "shells":
            self._selected_shell = self._shell_by_row.get(event.row_key)


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
