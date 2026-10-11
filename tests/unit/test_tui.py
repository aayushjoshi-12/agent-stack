"""Headless TUI tests: drive the Textual app with a fake Application.

No real model, store, or event bus: ``FakeApplication`` returns canned ``AgentEvent``
streams and records calls, so the whole event→pane routing is exercised in milliseconds.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from textual.widgets import DataTable, Input, RichLog, Tree

from agent_stack.core.events import EventType
from agent_stack.gateways.tui.app import TuiApp
from tests.unit.gateway_fakes import FakeApplication, event


def _log_text(widget: RichLog) -> str:
    return "\n".join(str(strip.text) for strip in widget.lines)


async def _submit(pilot, text: str) -> None:
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
            event(EventType.TOOL_STARTED, tool="run_command", tool_call_id="c1"),
            event(EventType.TOOL_COMPLETED, tool="run_command", tool_call_id="c1"),
            event(EventType.RUN_COMPLETED, steps=1, tool_calls=1),
        ]
    )
    app = TuiApp(application, session_id="s1")  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await _submit(pilot, "hi")
        text = _log_text(app.query_one("#chat", RichLog))

    assert "hello world" in text
    assert "run_command" in text
    assert "✓" in text
    # The run row was tracked and settled.
    assert application.sent[0][2] is None


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

    async with app.run_test() as pilot:
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
        assert "✓" in str(tree.root.children[0].label)


async def test_tui_shell_pane_streams_tool_output() -> None:
    application = FakeApplication()
    app = TuiApp(application, session_id="s1")  # type: ignore[arg-type]

    async with app.run_test() as pilot:
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
                chunk="second chunk\n",
            )
        )
        app.handle_event(event(EventType.TOOL_COMPLETED, tool="run_command", tool_call_id="c9"))
        await pilot.pause()

        assert app.shells["c9"].output == "first chunk\nsecond chunk\n"
        table = app.query_one("#shells", DataTable)
        row = table.get_row(app._shell_rows["c9"])
        assert row[0] == "run_command"
        assert row[1] == "done"
        assert "first chunk" in str(row[2])


async def test_tui_audit_pane_tails_run(tmp_workspace: Path) -> None:
    logs = tmp_workspace / ".agent" / "logs"
    logs.mkdir(parents=True)
    audit = logs / "audit.jsonl"
    audit.write_text(
        "\n".join(
            json.dumps(record)
            for record in (
                {"run_id": "A", "sequence": 0, "type": "run.started", "data": {}},
                {"run_id": "B", "sequence": 0, "type": "run.started", "data": {}},
                {"run_id": "A", "sequence": 1, "type": "tool.started", "data": {"tool": "x"}},
            )
        )
        + "\n"
    )

    application = FakeApplication(workspace=tmp_workspace)
    app = TuiApp(application, session_id="s1")  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        app.query_one("#panes").active = "tab-audit"
        await pilot.pause()
        app._active_run_id = "A"
        app.poll_audit()
        await pilot.pause()
        text = _log_text(app.query_one("#audit", RichLog))

    assert "tool.started" in text
    assert text.count("run.started") == 1  # only run A's line, not run B's
