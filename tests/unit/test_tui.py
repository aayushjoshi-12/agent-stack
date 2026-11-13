"""Headless TUI tests: drive the Textual app with a fake Application.

No real model, store, or event bus: ``FakeApplication`` returns canned ``AgentEvent``
streams and records calls, so the whole event→conversation routing is exercised in
milliseconds. The contract under test (ADR 012): user/assistant rows, collapsible
thinking and tool cards with live status, subagent rows updated in place, a multi-line
composer, breakpoint-driven responsive layout, a help overlay, and named themes.
"""

from __future__ import annotations

import asyncio
from typing import Any

from textual.containers import VerticalScroll

from agent_stack.core.events import EventType
from agent_stack.gateways.base import HistoryItem, SessionSummary
from agent_stack.gateways.tui.app import TuiApp
from agent_stack.gateways.tui.render import diff_kind, output_line_text
from agent_stack.gateways.tui.widgets import (
    AgentMessage,
    Disclosure,
    HeaderBar,
    HelpScreen,
    Notice,
    PromptArea,
    SessionPicker,
    SubagentRow,
    UserMessage,
    Welcome,
    format_duration,
)
from tests.unit.gateway_fakes import FakeApplication, event


def _chat_text(app: TuiApp) -> str:
    """Every leaf renderable in the conversation, in order (must run while mounted)."""
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


async def _settle(pilot: Any, n: int = 6) -> None:
    for _ in range(n):
        await pilot.pause()


async def _submit(pilot: Any, text: str) -> None:
    prompt = pilot.app.query_one("#prompt", PromptArea)
    prompt.load_text(text)
    await pilot.press("enter")
    await _settle(pilot)


def _app(application: FakeApplication | None = None) -> TuiApp:
    return TuiApp(application or FakeApplication(), session_id="s1")  # type: ignore[arg-type]


# ── conversation ─────────────────────────────────────────────────────────────
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
    app = _app(application)

    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "hi")
        text = _chat_text(app)
        assert len(app.query(UserMessage)) == 1
        assert len(app.query(AgentMessage)) == 1

    assert "hi" in text  # the user's own message
    assert "hello world" in text
    assert "run_command" in text
    assert "ls -la" in text  # the bash command itself is shown
    assert "run completed" not in text  # successful runs stay silent
    assert application.sent[0][2] is None


async def test_tui_renders_assistant_markdown_with_headings_and_code() -> None:
    content = (
        "# Summary\n\nSome **bold** text and `inline code`.\n\n```python\nx = 1\nprint(x)\n```"
    )
    application = FakeApplication(
        events=[
            event(EventType.RUN_STARTED),
            event(EventType.ASSISTANT_MESSAGE, content=content),
            event(EventType.RUN_COMPLETED, steps=1, tool_calls=0),
        ]
    )
    app = _app(application)

    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "hi")
        text = _chat_text(app)
        markdown_count = len(app.query("#chat Markdown"))

    assert "Summary" in text
    assert "bold" in text
    assert "x = 1" in text
    assert markdown_count == 1


async def test_tui_renders_collapsible_thinking_card() -> None:
    application = FakeApplication(
        events=[
            event(EventType.RUN_STARTED),
            event(EventType.MODEL_REASONING, text="weighing options"),
            event(EventType.ASSISTANT_MESSAGE, content="the answer"),
            event(EventType.RUN_COMPLETED, steps=1, tool_calls=0),
        ]
    )
    app = _app(application)

    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "hi")
        card = app.query_one(Disclosure)
        assert card.kind == "think"
        assert not card.has_class("-open")  # thinking starts collapsed

        card.toggle()
        await _settle(pilot, 3)
        assert card.has_class("-open")
        text = _chat_text(app)

    assert "thinking" in text
    assert "weighing options" in text
    assert "the answer" in text


async def test_thinking_header_is_painted_without_any_interaction() -> None:
    """Regression: a think card is never ticked, so its header must paint on mount."""
    app = _app()

    async with app.run_test(size=(140, 40)) as pilot:
        app.handle_event(event(EventType.MODEL_REASONING, text="weighing options"))
        await _settle(pilot, 3)
        header = app.query_one(Disclosure).query_one(".dh-main")
        assert "thinking" in str(header.render())


async def test_tui_run_failure_shows_error_notice_and_state() -> None:
    application = FakeApplication(
        events=[event(EventType.RUN_STARTED), event(EventType.RUN_FAILED, error="boom")]
    )
    app = _app(application)

    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "hi")
        notices = list(app.query(Notice))
        text = _chat_text(app)
        assert len(notices) == 1
        assert not app._run_active
        assert app._last_outcome == "failed"

    assert "error: boom" in text


# ── tool cards ───────────────────────────────────────────────────────────────
async def test_tool_card_streams_output_and_diff_colours_it() -> None:
    app = _app()

    async with app.run_test(size=(140, 40)) as pilot:
        app.handle_event(
            event(
                EventType.TOOL_STARTED,
                tool="run_command",
                tool_call_id="c9",
                args={"command": "git diff"},
            )
        )
        card = app.cards["c9"]
        assert card.status == "running"
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
        await _settle(pilot, 3)

        assert card.status == "done"
        assert card.diff is True  # the stream looked like unified-diff output
        assert card.has_class("-open")  # short output stays visible
        text = _chat_text(app)
        assert "git diff" in text
        assert "new line" in text
        assert "old line" in text

    assert diff_kind("+new line") == "add"
    assert diff_kind("-old line") == "del"
    assert diff_kind("@@ -1 +1 @@") == "hunk"
    assert diff_kind("diff --git a/x b/x") == "meta"
    assert diff_kind("plain") == "ctx"
    assert str(output_line_text("+new line", diff=True).style) == "green"
    assert str(output_line_text("plain output", diff=False).style) == "dim"


async def test_non_shell_tool_card_is_expandable_and_shows_its_arguments() -> None:
    app = _app()

    async with app.run_test(size=(140, 40)) as pilot:
        app.handle_event(
            event(
                EventType.TOOL_STARTED,
                tool="read_file",
                tool_call_id="r",
                args={"path": "src/x.py", "limit": 50},
            )
        )
        app.handle_event(event(EventType.TOOL_COMPLETED, tool="read_file", tool_call_id="r"))
        await _settle(pilot, 3)
        card = app.cards["r"]
        assert card.has_body  # so the header shows a chevron
        assert "▸" in str(card.query_one(".dh-main").render())
        assert not card.has_class("-open")  # arguments alone do not open the card

        card.toggle()
        await _settle(pilot, 3)
        body = str(card.query_one(".dh-body").render())
        assert "path: src/x.py" in body and "limit: 50" in body


async def test_footer_tooltips_are_effectively_disabled() -> None:
    """A tooltip over the composer hid the input on short terminals."""
    assert TuiApp.TOOLTIP_DELAY >= 600


async def test_help_overlay_keeps_the_dimmed_backdrop() -> None:
    application = FakeApplication()
    application.config.theme = "agent-stack-dark"  # ansi themes make modals transparent
    app = _app(application)

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("f1")
        await pilot.pause()
        background = app.screen.styles.background
        assert 0 < background.a < 1  # translucent, so the app stays faintly visible


async def test_tool_card_holds_partial_lines_until_complete() -> None:
    app = _app()

    async with app.run_test(size=(140, 40)) as pilot:
        app.handle_event(
            event(
                EventType.TOOL_STARTED, tool="run_command", tool_call_id="p", args={"command": "x"}
            )
        )
        card = app.cards["p"]
        app.handle_event(event(EventType.TOOL_OUTPUT, tool_call_id="p", chunk="par"))
        assert card.line_count == 0  # a half line is held back
        app.handle_event(event(EventType.TOOL_OUTPUT, tool_call_id="p", chunk="tial\nnext"))
        assert card.line_count == 1
        app.handle_event(event(EventType.TOOL_COMPLETED, tool="run_command", tool_call_id="p"))
        await _settle(pilot, 3)
        assert card.line_count == 2  # the trailing partial is flushed on completion


async def test_long_output_is_capped_and_folds_after_completion() -> None:
    app = _app()

    async with app.run_test(size=(140, 40)) as pilot:
        app.handle_event(
            event(
                EventType.TOOL_STARTED,
                tool="run_command",
                tool_call_id="l",
                args={"command": "seq"},
            )
        )
        card = app.cards["l"]
        chunk = "".join(f"line {i}\n" for i in range(1000))
        app.handle_event(event(EventType.TOOL_OUTPUT, tool_call_id="l", chunk=chunk))
        app.handle_event(event(EventType.TOOL_COMPLETED, tool="run_command", tool_call_id="l"))
        await _settle(pilot, 3)

        assert card.line_count == 1000  # nothing is miscounted...
        assert len(card._lines) < 1000  # ...but memory is bounded
        assert not card.has_class("-open")  # long output folds once finished
        card.toggle()
        await _settle(pilot, 3)
        assert card.has_class("-open")
        assert "earlier lines hidden" in str(card.query_one(".dh-body").render())


async def test_enter_on_focused_card_header_toggles_it() -> None:
    app = _app()

    async with app.run_test(size=(140, 40)) as pilot:
        app.handle_event(
            event(
                EventType.TOOL_STARTED, tool="run_command", tool_call_id="t", args={"command": "x"}
            )
        )
        app.handle_event(event(EventType.TOOL_OUTPUT, tool_call_id="t", chunk="a\n" * 20))
        app.handle_event(event(EventType.TOOL_COMPLETED, tool="run_command", tool_call_id="t"))
        await _settle(pilot, 3)
        card = app.cards["t"]
        assert not card.has_class("-open")

        card.query_one(".dh-header").focus()
        await pilot.press("enter")
        await _settle(pilot, 3)
        assert card.has_class("-open")


# ── subagents ────────────────────────────────────────────────────────────────
async def test_subagent_row_is_updated_in_place() -> None:
    app = _app()

    async with app.run_test(size=(140, 40)) as pilot:
        app.handle_event(
            event(EventType.SUBAGENT_STARTED, child_run_id="child-abc", agent_name="explorer")
        )
        await _settle(pilot, 3)
        assert app._last_child_id == "child-abc"
        rows = list(app.query(SubagentRow))
        assert len(rows) == 1 and rows[0].running

        app.handle_event(
            event(
                EventType.SUBAGENT_COMPLETED,
                child_run_id="child-abc",
                agent_name="explorer",
                status="completed",
            )
        )
        await _settle(pilot, 3)
        assert len(app.query(SubagentRow)) == 1  # same row, not a second one
        assert not rows[0].running
        text = _chat_text(app)

    assert "explorer" in text
    assert "done" in text


# ── composer ─────────────────────────────────────────────────────────────────
async def test_ctrl_j_inserts_newline_and_enter_sends() -> None:
    application = FakeApplication(events=[event(EventType.RUN_STARTED)])
    app = _app(application)

    async with app.run_test(size=(140, 40)) as pilot:
        prompt = app.query_one("#prompt", PromptArea)
        await pilot.press("a", "ctrl+j", "b")
        assert prompt.text == "a\nb"
        await pilot.press("enter")
        await _settle(pilot)
        assert prompt.text == ""
        assert application.sent  # one run was started with the multi-line text


async def test_prompt_history_recall_with_up_and_down() -> None:
    app = _app(
        FakeApplication(events=[event(EventType.RUN_STARTED), event(EventType.RUN_COMPLETED)])
    )

    async with app.run_test(size=(140, 40)) as pilot:
        prompt = app.query_one("#prompt", PromptArea)
        await _submit(pilot, "first")
        await _submit(pilot, "second")
        await pilot.press("up")
        assert prompt.text == "second"
        await pilot.press("up")
        assert prompt.text == "first"
        await pilot.press("down", "down")
        assert prompt.text == ""  # back to the (empty) draft


async def test_submit_is_blocked_while_a_run_is_active() -> None:
    gate = asyncio.Event()
    application = FakeApplication(events=[event(EventType.RUN_STARTED)], gate=gate)
    app = _app(application)

    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "first")
        assert app._run_active
        await _submit(pilot, "second")
        assert len(application.sent) == 1  # the second message was not started
        assert app.query_one("#prompt", PromptArea).text == "second"  # and is not lost
        gate.set()
        await _settle(pilot)


# ── cancel / quit ────────────────────────────────────────────────────────────
async def test_ctrl_c_cancels_run_then_quits() -> None:
    gate = asyncio.Event()
    application = FakeApplication(events=[event(EventType.RUN_STARTED)], gate=gate)
    app = _app(application)

    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "long task")
        run_id = application.sent[0][1]
        assert app._active_run_id == run_id

        await pilot.press("ctrl+c")
        await pilot.pause()
        assert application.cancelled == [run_id]
        assert app._cancel_sent

        gate.set()
        await pilot.pause()


async def test_escape_cancels_a_live_run() -> None:
    gate = asyncio.Event()
    application = FakeApplication(events=[event(EventType.RUN_STARTED)], gate=gate)
    app = _app(application)

    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "long task")
        run_id = application.sent[0][1]
        await pilot.press("escape")
        await pilot.pause()
        assert application.cancelled == [run_id]
        gate.set()
        await pilot.pause()


async def test_escape_returns_focus_to_the_prompt_when_idle() -> None:
    app = _app()

    async with app.run_test(size=(140, 40)) as pilot:
        app.query_one("#chat").focus()
        await pilot.pause()
        assert app.focused is not app.query_one("#prompt")
        await pilot.press("escape")
        await pilot.pause()
        assert app.focused is app.query_one("#prompt")


# ── chrome: title, header state, sidebar ─────────────────────────────────────
async def test_first_message_sets_title_and_sidebar_usage() -> None:
    app = _app(FakeApplication(events=[event(EventType.RUN_STARTED)], gate=asyncio.Event()))

    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "make the panels slightly darker")
        assert "panels slightly darker" in str(app.query_one("#title").render())
        assert "working" in str(app.query_one("#run-state").render())
        side = str(app.query_one("#sb-context").render())
        assert "200K" in side
        assert "status: working" in str(app.query_one("#sb-activity").render())


async def test_header_shows_done_after_a_completed_run() -> None:
    app = _app(
        FakeApplication(events=[event(EventType.RUN_STARTED), event(EventType.RUN_COMPLETED)])
    )

    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "hi")
        assert "done" in str(app.query_one("#run-state").render())
        assert app.query_one(HeaderBar)


async def test_welcome_shows_when_empty() -> None:
    app = _app()

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        assert len(app.query(Welcome)) == 1  # the empty state explains the basics


async def test_new_conversation_starts_a_new_session_and_keeps_the_old_one() -> None:
    application = FakeApplication(
        events=[event(EventType.RUN_STARTED), event(EventType.RUN_COMPLETED)]
    )
    app = _app(application)

    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "first task")
        assert len(app.query(Welcome)) == 0
        assert app._tokens > 0
        first = app._session_id
        assert application.titles[first] == "first task"  # persisted so it can be found

        await pilot.press("ctrl+n")
        await _settle(pilot, 3)

        assert app._session_id != first  # a real new session, not just a cleared screen
        assert len(app.query(Welcome)) == 1
        assert len(app.query(UserMessage)) == 0
        assert app._tokens == 0
        assert app._task_title is None
        assert "New conversation" in str(app.query_one("#title").render())


async def test_new_conversation_is_refused_while_a_run_is_active() -> None:
    gate = asyncio.Event()
    app = _app(FakeApplication(events=[event(EventType.RUN_STARTED)], gate=gate))

    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "busy")
        before = app._session_id
        await pilot.press("ctrl+n")
        await _settle(pilot, 3)
        assert app._session_id == before
        assert len(app.query(UserMessage)) == 1
        gate.set()
        await _settle(pilot)


async def test_switching_conversation_replays_its_history() -> None:
    application = FakeApplication()
    application.sessions = [
        SessionSummary(id="old-1", title="Fix the retry bug"),
        SessionSummary(id="old-2", title="Other"),
    ]
    application.histories["old-1"] = [
        HistoryItem("user", "fix the retry bug"),
        HistoryItem("tool", "read_file", {"path": "fetch.py"}),
        HistoryItem("assistant", "Done, I added backoff."),
    ]
    app = _app(application)

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("ctrl+o")
        await _settle(pilot, 4)
        assert isinstance(app.screen, SessionPicker)

        await pilot.press("enter")  # the first (most recent) entry is highlighted
        await _settle(pilot, 8)

        assert not isinstance(app.screen, SessionPicker)
        assert app._session_id == "old-1"
        assert app._task_title == "Fix the retry bug"
        assert len(app.query(Welcome)) == 0
        assert len(app.query(UserMessage)) == 1
        assert len(app.query(AgentMessage)) == 1
        card = app.query_one(Disclosure)
        assert card.has_body  # arguments are available behind the chevron
        text = _chat_text(app)
        assert "fix the retry bug" in text and "added backoff" in text
        assert "Fix the retry bug" in str(app.query_one("#title").render())


async def test_picker_escape_keeps_the_current_conversation() -> None:
    application = FakeApplication()
    application.sessions = [SessionSummary(id="old-1", title="Earlier")]
    app = _app(application)

    async with app.run_test(size=(140, 40)) as pilot:
        current = app._session_id
        await pilot.press("ctrl+o")
        await _settle(pilot, 4)
        await pilot.press("escape")
        await _settle(pilot, 3)
        assert not isinstance(app.screen, SessionPicker)
        assert app._session_id == current


async def test_picker_with_no_conversations_says_so() -> None:
    app = _app()

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("ctrl+o")
        await _settle(pilot, 4)
        assert isinstance(app.screen, SessionPicker)
        await pilot.press("escape")


# ── responsive layout ────────────────────────────────────────────────────────
async def test_sidebar_follows_breakpoints() -> None:
    app = _app()

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        sidebar = app.query_one("#sidebar")
        assert sidebar.styles.display != "none"

        await pilot.resize_terminal(88, 24)
        await pilot.pause()
        assert sidebar.styles.display == "none"  # narrow: conversation only

        await pilot.resize_terminal(140, 40)
        await pilot.pause()
        assert sidebar.styles.display != "none"


async def test_ctrl_b_overrides_the_width_default() -> None:
    app = _app()

    async with app.run_test(size=(88, 24)) as pilot:
        await pilot.pause()
        sidebar = app.query_one("#sidebar")
        assert sidebar.styles.display == "none"

        await pilot.press("ctrl+b")
        await pilot.pause()
        assert sidebar.styles.display != "none"  # forced on at a narrow width

        await pilot.press("ctrl+b")
        await pilot.pause()
        assert sidebar.styles.display == "none"


async def test_ctrl_b_twice_returns_to_the_responsive_default() -> None:
    """Regression: show -> hide -> show must not pin the sidebar open on a narrow terminal."""
    app = _app()

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        sidebar = app.query_one("#sidebar")
        await pilot.press("ctrl+b", "ctrl+b")  # hide, then back to automatic
        await pilot.pause()
        assert sidebar.styles.display != "none"  # wide again

        await pilot.resize_terminal(70, 24)
        await pilot.pause()
        assert sidebar.styles.display == "none"  # automatic: narrow hides it


async def test_ctrl_b_can_hide_the_sidebar_on_a_wide_terminal() -> None:
    app = _app()

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        sidebar = app.query_one("#sidebar")
        await pilot.press("ctrl+b")
        await pilot.pause()
        assert sidebar.styles.display == "none"


# ── help, themes ─────────────────────────────────────────────────────────────
async def test_f1_opens_help_and_escape_closes_it() -> None:
    app = _app()

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("f1")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, HelpScreen)


async def test_default_theme_is_the_named_dark_theme() -> None:
    application = FakeApplication()
    application.config.theme = "agent-stack-dark"
    app = _app(application)

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        assert app.theme == "agent-stack-dark"
        assert app.current_theme.dark


async def test_ctrl_t_switches_between_dark_and_light() -> None:
    application = FakeApplication()
    application.config.theme = "agent-stack-dark"
    app = _app(application)

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert app.theme == "agent-stack-light"
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert app.theme == "agent-stack-dark"


async def test_ansi_theme_still_selectable_and_unknown_falls_back() -> None:
    application = FakeApplication()
    application.config.theme = "ansi-light"
    app = _app(application)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        assert app.theme == "ansi-light"
        assert app.current_theme.background == "ansi_default"

    application = FakeApplication()
    application.config.theme = "no-such-theme"
    app = _app(application)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        assert app.theme == "agent-stack-dark"


def test_format_duration_is_compact() -> None:
    assert format_duration(0.42) == "0.4s"
    assert format_duration(12.4) == "12s"
    assert format_duration(65) == "1m05s"
