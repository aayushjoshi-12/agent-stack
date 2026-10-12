"""Headless TUI tests: drive the Textual app with a fake Application.

No real model, store, or event bus: ``FakeApplication`` returns canned ``AgentEvent``
streams and records calls, so the whole event→conversation routing is exercised in
milliseconds. The design contract under test: one wide conversation column with a
context sidebar, sections separated by darker panels (never divider rules), Markdown
assistant messages, collapsible thinking, and inline command transcripts whose output
is diff-coloured when it looks like unified-diff text.
"""

from __future__ import annotations

import asyncio
from typing import Any

from textual.containers import VerticalScroll
from textual.widgets import Input, Static

from agent_stack.core.events import EventType
from agent_stack.gateways.tui.app import TuiApp
from agent_stack.gateways.tui.render import output_line_text
from tests.unit.gateway_fakes import FakeApplication, event


def _chat_text(app: TuiApp) -> str:
    """Every leaf renderable in the conversation, in order.

    Walks the widget tree under ``#chat`` and collects the plain text of widgets that
    render as text themselves (Statics, Markdown blocks, labels); containers like the
    Markdown root, collapsibles and command panels are descended into, not printed.
    Must run while the app is mounted (inside ``run_test``).
    """
    chat = app.query_one("#chat", VerticalScroll)
    parts: list[str] = []

    def _walk(widget: Any) -> None:
        rendered = widget.render()
        plain = getattr(rendered, "plain", None)
        if isinstance(plain, str) and plain.strip() and not widget.children:
            parts.append(plain)
        for child in widget.children:
            _walk(child)

    for child in chat.children:
        _walk(child)
    return "\n".join(parts)


async def _submit(pilot: Any, text: str) -> None:
    prompt = pilot.app.query_one("#prompt", Input)
    prompt.value = text
    await pilot.press("enter")
    for _ in range(6):
        await pilot.pause()


async def test_tui_renders_messages_and_tool_history() -> None:
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

    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "hi")
        text = _chat_text(app)

    assert "hi" in text  # the user's own message
    assert "hello world" in text
    assert "run_command" in text
    assert "ls -la" in text  # the bash command itself is shown
    assert "run completed" not in text  # successful runs stay silent
    assert application.sent[0][2] is None


async def test_tui_renders_assistant_markdown_with_headings_and_code() -> None:
    content = (
        "# Summary\n\nSome **bold** text and `inline code`.\n\n"
        "```python\nx = 1\nprint(x)\n```"
    )
    application = FakeApplication(
        events=[
            event(EventType.RUN_STARTED),
            event(EventType.ASSISTANT_MESSAGE, content=content),
            event(EventType.RUN_COMPLETED, steps=1, tool_calls=0),
        ]
    )
    app = TuiApp(application, session_id="s1")  # type: ignore[arg-type]

    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "hi")
        text = _chat_text(app)
        markdown_count = len(app.query("#chat Markdown"))

    assert "Summary" in text  # the Markdown heading
    assert "bold" in text
    assert "x = 1" in text  # the fenced code block (syntax-highlighted content)
    assert markdown_count == 1  # the message rendered as one Markdown widget


async def test_tui_renders_collapsible_thinking_row() -> None:
    application = FakeApplication(
        events=[
            event(EventType.RUN_STARTED),
            event(EventType.MODEL_REASONING, text="weighing options"),
            event(EventType.ASSISTANT_MESSAGE, content="the answer"),
            event(EventType.RUN_COMPLETED, steps=1, tool_calls=0),
        ]
    )
    app = TuiApp(application, session_id="s1")  # type: ignore[arg-type]

    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "hi")
        text = _chat_text(app)

    assert "Thinking" in text  # the collapsible's title
    assert "weighing options" in text
    assert "the answer" in text


async def test_tui_command_block_streams_and_diff_colors_output() -> None:
    application = FakeApplication()
    app = TuiApp(application, session_id="s1")  # type: ignore[arg-type]

    async with app.run_test(size=(140, 40)) as pilot:
        app.handle_event(
            event(
                EventType.TOOL_STARTED,
                tool="run_command",
                tool_call_id="c9",
                args={"command": "git diff"},
            )
        )
        app.handle_event(
            event(
                EventType.TOOL_OUTPUT,
                tool="run_command",
                tool_call_id="c9",
                chunk="diff --git a/x.py b/x.py\n",
            )
        )
        app.handle_event(
            event(
                EventType.TOOL_OUTPUT,
                tool="run_command",
                tool_call_id="c9",
                chunk="@@ -1 +1 @@\n+new line\n-old line\n",
            )
        )
        app.handle_event(event(EventType.TOOL_COMPLETED, tool="run_command", tool_call_id="c9"))
        for _ in range(3):
            await pilot.pause()

        state = app.shells["c9"]
        assert state.status == "done"
        assert state.diff is True  # the stream looked like unified-diff output
        text = _chat_text(app)
        assert "git diff" in text  # the command line heads the transcript panel
        assert "new line" in text
        assert "old line" in text
        # Diff lines are coloured per role, not left dim like plain output.
        assert str(output_line_text("+new line", diff=True).style) == "green"
        assert str(output_line_text("-old line", diff=True).style) == "red"
        assert str(output_line_text("@@ -1 +1 @@", diff=True).style) == "cyan"
        assert str(output_line_text("diff --git a/x b/x", diff=True).style) == "bold dim"
        assert str(output_line_text("plain output", diff=False).style) == "dim"


async def test_tui_ctrl_c_cancels_run() -> None:
    gate = asyncio.Event()
    application = FakeApplication(events=[event(EventType.RUN_STARTED)], gate=gate)
    app = TuiApp(application, session_id="s1")  # type: ignore[arg-type]

    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "long task")
        run_id = application.sent[0][1]
        assert app._active_run_id == run_id

        await pilot.press("ctrl+c")
        await pilot.pause()
        assert application.cancelled == [run_id]

        gate.set()
        await pilot.pause()


async def test_tui_renders_run_failure() -> None:
    application = FakeApplication(
        events=[
            event(EventType.RUN_STARTED),
            event(EventType.RUN_FAILED, error="boom"),
        ]
    )
    app = TuiApp(application, session_id="s1")  # type: ignore[arg-type]

    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "hi")
        text = _chat_text(app)

    assert "error: boom" in text


async def test_tui_shows_subagent_lifecycle_rows() -> None:
    application = FakeApplication()
    app = TuiApp(application, session_id="s1")  # type: ignore[arg-type]

    async with app.run_test(size=(140, 40)) as pilot:
        app.handle_event(
            event(EventType.SUBAGENT_STARTED, child_run_id="child-abc", agent_name="explorer")
        )
        for _ in range(3):
            await pilot.pause()
        assert app._last_child_id == "child-abc"

        app.handle_event(
            event(
                EventType.SUBAGENT_COMPLETED,
                child_run_id="child-abc",
                agent_name="explorer",
                status="completed",
            )
        )
        for _ in range(3):
            await pilot.pause()
        text = _chat_text(app)

    assert "explorer" in text
    assert "started" in text
    assert "done" in text


async def test_tui_sets_task_title_and_context_usage() -> None:
    application = FakeApplication(events=[event(EventType.RUN_STARTED)])
    app = TuiApp(application, session_id="s1")  # type: ignore[arg-type]

    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "make the panels slightly darker")

        # The first user message becomes the task title, in both title rows.
        assert "panels slightly darker" in str(app.query_one("#side-title", Static).render())
        assert "agent-stack" not in str(app.query_one("#side-title", Static).render())
        assert "panels slightly darker" in str(app.query_one("#title-text", Static).render())

        # Context usage is estimated from the conversation's text.
        tokens = str(app.query_one("#side-tokens", Static).render())
        used = str(app.query_one("#side-used", Static).render())
        status = str(app.query_one("#status-ctx", Static).render())
    assert "tokens" in tokens
    assert "% used" in used
    assert "K (" in status


async def test_tui_status_bar_shows_path_and_run() -> None:
    application = FakeApplication(events=[event(EventType.RUN_STARTED)])
    app = TuiApp(application, session_id="s1")  # type: ignore[arg-type]

    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "hi")
        path = str(app.query_one("#status-path", Static).render())
        run = str(app.query_one("#status-file", Static).render())

    assert path  # the workspace path, possibly with a branch suffix
    assert run.startswith("run #")  # the active run id while the run is live


async def test_tui_clear_chat_starts_fresh_conversation() -> None:
    application = FakeApplication(events=[event(EventType.RUN_STARTED)])
    app = TuiApp(application, session_id="s1")  # type: ignore[arg-type]

    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "first task")
        chat = app.query_one("#chat", VerticalScroll)
        assert len(chat.children) > 0

        app.action_clear_chat()
        await pilot.pause()

        assert len(chat.children) == 0
        assert str(app.query_one("#title-text", Static).render()) == "agent-stack"
        assert app._tokens == 0


async def test_tui_has_no_welcome_banner() -> None:
    """The empty conversation starts blank: no ASCII art, no banner."""
    app = TuiApp(FakeApplication(), session_id="s1")  # type: ignore[arg-type]

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        assert len(app.query(".welcome")) == 0
        assert len(app.query_one("#chat", VerticalScroll).children) == 0


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


async def test_tui_hides_sidebar_on_narrow_terminals() -> None:
    application = FakeApplication()
    app = TuiApp(application, session_id="s1")  # type: ignore[arg-type]

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        sidebar = app.query_one("#sidebar")
        assert sidebar.styles.display != "none"

        await pilot.resize_terminal(88, 24)
        await pilot.pause()
        # Narrow terminals drop the context sidebar, keeping the conversation readable.
        assert sidebar.styles.display == "none"

        await pilot.resize_terminal(140, 40)
        await pilot.pause()
        assert sidebar.styles.display != "none"


async def test_tui_toggle_sidebar_forces_it_back() -> None:
    application = FakeApplication()
    app = TuiApp(application, session_id="s1")  # type: ignore[arg-type]

    async with app.run_test(size=(88, 24)) as pilot:
        await pilot.pause()
        sidebar = app.query_one("#sidebar")
        assert sidebar.styles.display == "none"  # collapsed by default at this width

        await pilot.press("ctrl+b")
        await pilot.pause()
        # The explicit toggle wins over the width heuristic, so the sidebar stays usable.
        assert sidebar.styles.display != "none"

        await pilot.press("ctrl+b")
        await pilot.pause()
        assert sidebar.styles.display == "none"