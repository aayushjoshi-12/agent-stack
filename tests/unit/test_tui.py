"""Headless TUI tests: drive the Textual app with a fake Application.

No real model, store, or event bus: ``FakeApplication`` returns canned ``AgentEvent``
streams and records calls, so the whole event→pane routing is exercised in milliseconds.
"""

from __future__ import annotations

import asyncio
from typing import Any

from textual.containers import VerticalScroll
from textual.widgets import Input, RichLog, Static, Tree

from agent_stack.core.events import EventType
from agent_stack.gateways.tui.app import Divider, TuiApp
from tests.unit.gateway_fakes import FakeApplication, event


def _log_text(widget: RichLog) -> str:
    return "\n".join(str(strip.text) for strip in widget.lines)


def _chat_text(app: TuiApp) -> str:
    """Every static row in the conversation, in order (collapsibles included)."""
    chat = app.query_one("#chat", VerticalScroll)
    return "\n".join(str(widget.render()) for widget in chat.query("*") if isinstance(widget, Static))


class _FakeMouse:
    """The slice of MouseEvent the Divider drag handlers read."""

    def __init__(self, x: int) -> None:
        self.screen_x = x

    def stop(self) -> None:
        return None


async def _submit(pilot: Any, text: str) -> None:
    prompt = pilot.app.query_one("#prompt", Input)
    prompt.value = text
    await pilot.press("enter")
    for _ in range(6):
        await pilot.pause()


async def test_tui_renders_assistant_message_and_tool_status() -> None:
    application = FakeApplication(
        events=[
            event(EventType.RUN_STARTED),
            event(EventType.ASSISTANT_MESSAGE, content="hello world", tool_calls=["run_command"]),
            event(
                EventType.TOOL_STARTED,
                tool="run_command",
                tool_call_id="c1",
                args={"command": "ls -la"},
            ),
            event(EventType.TOOL_COMPLETED, tool="run_command", tool_call_id="c1"),
            event(EventType.RUN_COMPLETED, steps=1, tool_calls=1),
        ]
    )
    app = TuiApp(application, session_id="s1")  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await _submit(pilot, "hi")
        text = _chat_text(app)

    assert "hi" in text  # the user's own message
    assert "hello world" in text
    assert "run_command" in text
    assert "ls -la" in text  # the bash command itself is shown
    assert "run completed" not in text  # successful runs stay silent
    # The run row was tracked and settled.
    assert application.sent[0][2] is None


async def test_tui_renders_reasoning_block() -> None:
    application = FakeApplication(
        events=[
            event(EventType.RUN_STARTED),
            event(EventType.MODEL_REASONING, text="weighing options"),
            event(EventType.ASSISTANT_MESSAGE, content="the answer"),
            event(EventType.RUN_COMPLETED, steps=1, tool_calls=0),
        ]
    )
    app = TuiApp(application, session_id="s1")  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await _submit(pilot, "hi")
        text = _chat_text(app)

    assert "thinking" in text  # the collapsible's title
    assert "weighing options" in text
    assert "the answer" in text


async def test_tui_ctrl_c_cancels_run() -> None:
    gate = asyncio.Event()
    application = FakeApplication(events=[event(EventType.RUN_STARTED)], gate=gate)
    app = TuiApp(application, session_id="s1")  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await _submit(pilot, "long task")
        run_id = application.sent[0][1]
        assert app._active_run_id == run_id

        await pilot.press("ctrl+c")
        await pilot.pause()
        assert application.cancelled == [run_id]

        gate.set()
        await pilot.pause()


async def test_tui_subagent_events_nest_under_parent() -> None:
    child_id = "child-abc"
    application = FakeApplication(
        child_events={
            child_id: [
                event(EventType.RUN_STARTED, run_id=child_id, parent_run_id="run-1"),
                event(
                    EventType.ASSISTANT_MESSAGE,
                    run_id=child_id,
                    parent_run_id="run-1",
                    content="exploring the repo",
                ),
            ]
        }
    )
    app = TuiApp(application, session_id="s1")  # type: ignore[arg-type]

    async with app.run_test(size=(140, 40)) as pilot:
        app.handle_event(
            event(EventType.SUBAGENT_STARTED, child_run_id=child_id, agent_name="explorer")
        )
        for _ in range(4):
            await pilot.pause()

        tree = app.query_one("#subagents", Tree)
        assert len(tree.root.children) == 1
        assert "explorer" in str(tree.root.children[0].label)

        app._select_child(child_id)
        await pilot.pause()
        assert "exploring the repo" in _log_text(app.query_one("#subagent-log", RichLog))

        app.handle_event(
            event(
                EventType.SUBAGENT_COMPLETED,
                child_run_id=child_id,
                agent_name="explorer",
                status="completed",
            )
        )
        assert "done" in str(tree.root.children[0].label)


async def test_tui_shell_pane_streams_tool_output() -> None:
    application = FakeApplication()
    app = TuiApp(application, session_id="s1")  # type: ignore[arg-type]

    async with app.run_test(size=(140, 40)) as pilot:
        app.query_one("#panes").active = "tab-shells"
        app.handle_event(event(EventType.TOOL_STARTED, tool="run_command", tool_call_id="c9"))
        app.handle_event(
            event(
                EventType.TOOL_OUTPUT,
                tool="run_command",
                tool_call_id="c9",
                stream="stdout",
                chunk="first chunk\n",
            )
        )
        app.handle_event(
            event(
                EventType.TOOL_OUTPUT,
                tool="run_command",
                tool_call_id="c9",
                stream="stdout",
                chunk="second chunk",
            )
        )
        app.handle_event(event(EventType.TOOL_COMPLETED, tool="run_command", tool_call_id="c9"))
        for _ in range(3):
            await pilot.pause()

        # The whole (untruncated) output is retained and streamed to the shell log.
        assert app.shells["c9"].output == "first chunk\nsecond chunk"
        assert app.shells["c9"].status == "done"
        log = _log_text(app.query_one("#shell-log", RichLog))
        assert "first chunk" in log
        assert "second chunk" in log
        node = app.query_one("#shells", Tree).root.children[0]
        assert "run_command" in str(node.label)
        assert "done" in str(node.label)


async def test_tui_tree_selection_dispatches_by_pane() -> None:
    """Selecting a node in either pane tree switches that pane (not the other)."""
    application = FakeApplication()
    app = TuiApp(application, session_id="s1")  # type: ignore[arg-type]

    async with app.run_test(size=(140, 40)) as pilot:
        app.query_one("#panes").active = "tab-shells"
        app.handle_event(event(EventType.TOOL_STARTED, tool="run_command", tool_call_id="c1"))
        app.handle_event(
            event(EventType.TOOL_OUTPUT, tool="run_command", tool_call_id="c1", chunk="alpha\n")
        )
        app.handle_event(event(EventType.TOOL_STARTED, tool="run_command2", tool_call_id="c2"))
        app.handle_event(
            event(EventType.TOOL_OUTPUT, tool="run_command2", tool_call_id="c2", chunk="beta\n")
        )
        for _ in range(2):
            await pilot.pause()

        # c1 is selected first; re-selecting c2 must swap the shell log over to it.
        shells = app.query_one("#shells", Tree)
        assert "alpha" in _log_text(app.query_one("#shell-log", RichLog))
        shells.post_message(Tree.NodeSelected(app._shell_nodes["c2"]))
        await pilot.pause()
        log = _log_text(app.query_one("#shell-log", RichLog))
        assert "beta" in log
        assert "alpha" not in log

        # The subagents tree routes to the subagent log instead.
        app.query_one("#panes").active = "tab-subagents"
        await pilot.pause()
        child_id = "child-1"
        app.handle_event(
            event(EventType.SUBAGENT_STARTED, child_run_id=child_id, agent_name="explorer")
        )
        app.handle_event(
            event(
                EventType.ASSISTANT_MESSAGE,
                run_id=child_id,
                parent_run_id="run-1",
                content="exploring the repo",
            )
        )
        subagents = app.query_one("#subagents", Tree)
        subagents.post_message(Tree.NodeSelected(app._child_nodes[child_id]))
        await pilot.pause()
        assert "exploring the repo" in _log_text(app.query_one("#subagent-log", RichLog))


async def test_tui_divider_drag_resizes_pane() -> None:
    application = FakeApplication()
    app = TuiApp(application, session_id="s1")  # type: ignore[arg-type]

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        divider = app.query_one("#divider-left", Divider)
        left = app.query_one("#left")
        before = left.outer_size.width

        divider.on_mouse_down(_FakeMouse(10))  # type: ignore[arg-type]
        divider.on_mouse_move(_FakeMouse(22))  # type: ignore[arg-type]
        await pilot.pause()
        assert left.outer_size.width == before + 12

        divider.on_mouse_up(_FakeMouse(22))  # type: ignore[arg-type]
        await pilot.pause()


async def test_tui_removes_the_welcome_banner_on_first_message() -> None:
    application = FakeApplication(events=[event(EventType.RUN_STARTED)])
    app = TuiApp(application, session_id="s1")  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        assert len(app.query(".welcome")) == 1
        await _submit(pilot, "hi")
        assert len(app.query(".welcome")) == 0


async def test_tui_uses_configured_theme() -> None:
    application = FakeApplication()
    application.config.theme = "ansi-light"
    app = TuiApp(application, session_id="s1")  # type: ignore[arg-type]

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        assert app.theme == "ansi-light"
        # The terminal-native theme keeps the terminal's own background (no hardcoded
        # palette), so the app renders in whatever colours the user's terminal uses.
        assert app.current_theme.background == "ansi_default"


async def test_tui_collapses_panes_as_the_terminal_narrows() -> None:
    application = FakeApplication()
    app = TuiApp(application, session_id="s1")  # type: ignore[arg-type]

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        panes = app.query_one("#panes")
        left = app.query_one("#left")
        assert panes.styles.display == "block"
        assert left.styles.display == "block"

        await pilot.resize_terminal(88, 24)
        await pilot.pause()
        # Right panes collapse first, keeping the conversation readable.
        assert panes.styles.display == "none"
        assert left.styles.display == "block"

        await pilot.resize_terminal(60, 20)
        await pilot.pause()
        # Then the session sidebar, leaving just the conversation and input.
        assert left.styles.display == "none"

        await pilot.resize_terminal(140, 40)
        await pilot.pause()
        assert panes.styles.display == "block"


async def test_tui_toggle_panes_reveals_collapsed_panes() -> None:
    application = FakeApplication()
    app = TuiApp(application, session_id="s1")  # type: ignore[arg-type]

    async with app.run_test(size=(88, 24)) as pilot:
        await pilot.pause()
        panes = app.query_one("#panes")
        assert panes.styles.display == "none"  # collapsed by default at this width

        await pilot.press("ctrl+b")
        await pilot.pause()
        # The explicit toggle wins over the width heuristic, so the panes stay reachable.
        assert panes.styles.display == "block"

        await pilot.press("ctrl+b")
        await pilot.pause()
        assert panes.styles.display == "none"
