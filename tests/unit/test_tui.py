"""Headless TUI tests: drive the Textual app with a fake Application.

No real model, store, or event bus: ``FakeApplication`` returns canned ``AgentEvent``
streams and records calls, so the whole event→conversation routing is exercised in
milliseconds. The contract under test (ADR 012): user/assistant rows, collapsible
thinking and tool cards with live status, subagent rows updated in place, a multi-line
composer, breakpoint-driven responsive layout, a help overlay, and named themes.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from agent_stack.core.events import EventType
from agent_stack.gateways.base import HistoryItem, SessionSummary
from agent_stack.gateways.tui.app import TuiApp
from agent_stack.gateways.tui.chrome import HelpScreen, PromptArea, Welcome
from agent_stack.gateways.tui.render import diff_kind
from agent_stack.gateways.tui.rows import (
    AgentMessage,
    Disclosure,
    Notice,
    SubagentRow,
    UserMessage,
    format_duration,
)
from agent_stack.gateways.tui.screens import ApprovalScreen, SessionPicker
from tests.unit.gateway_fakes import FakeApplication, event


def _tabs_text(app: TuiApp) -> str:
    """The tab bar's labels, as shown."""
    return " | ".join(str(tab.render()) for tab in app.query(".tab"))


def _chat_text(app: TuiApp) -> str:
    """Every leaf renderable in the conversation, in order (must run while mounted)."""
    chat = app.tab
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
        assert len(app.tab.query(UserMessage)) == 1
        assert len(app.tab.query(AgentMessage)) == 1

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
        markdown_count = len(app.tab.query("Markdown"))

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
        card = app.tab.query_one(Disclosure)
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
        header = app.tab.query_one(Disclosure).query_one(".dh-main")
        assert "thinking" in str(header.render())


async def test_tui_run_failure_shows_error_notice_and_state() -> None:
    application = FakeApplication(
        events=[event(EventType.RUN_STARTED), event(EventType.RUN_FAILED, error="boom")]
    )
    app = _app(application)

    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "hi")
        notices = list(app.tab.query(Notice))
        text = _chat_text(app)
        assert len(notices) == 1
        assert not app.tab.running
        assert app.tab.outcome == "failed"

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
        card = app.tab.cards["c9"]
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
        card = app.tab.cards["r"]
        assert card.has_body  # so the header shows a chevron
        assert "▸" in str(card.query_one(".dh-main").render())
        assert not card.has_class("-open")  # arguments alone do not open the card

        card.toggle()
        await _settle(pilot, 3)
        body = str(card.query_one(".dh-body").render())
        assert "path: src/x.py" in body and "limit: 50" in body


async def test_help_overlay_keeps_the_dimmed_backdrop() -> None:
    app = _app()

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("f1")
        await pilot.pause()
        background = app.screen.styles.background
        assert 0 < background.a < 1  # translucent, so the app stays faintly visible


async def test_a_failed_tool_shows_failed_and_stays_open() -> None:
    app = _app()

    async with app.run_test(size=(140, 40)) as pilot:
        app.handle_event(
            event(
                EventType.TOOL_STARTED, tool="run_command", tool_call_id="f", args={"command": "x"}
            )
        )
        app.handle_event(
            event(
                EventType.TOOL_COMPLETED,
                tool="run_command",
                tool_call_id="f",
                ok=False,
                error="exit 2",
            )
        )
        await _settle(pilot, 3)
        card = app.tab.cards["f"]
        assert card.status == "failed"
        assert card.has_class("-failed")
        assert "✗" in str(card.query_one(".dh-main").render())


async def test_tool_card_holds_partial_lines_until_complete() -> None:
    app = _app()

    async with app.run_test(size=(140, 40)) as pilot:
        app.handle_event(
            event(
                EventType.TOOL_STARTED, tool="run_command", tool_call_id="p", args={"command": "x"}
            )
        )
        card = app.tab.cards["p"]
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
        card = app.tab.cards["l"]
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
        card = app.tab.cards["t"]
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
        assert app.tab.last_child_id == "child-abc"
        rows = list(app.tab.query(SubagentRow))
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
        assert len(app.tab.query(SubagentRow)) == 1  # same row, not a second one
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


async def test_submit_while_a_run_is_active_queues_into_it() -> None:
    gate = asyncio.Event()
    application = FakeApplication(events=[event(EventType.RUN_STARTED)], gate=gate)
    app = _app(application)

    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "first")
        assert app.tab.running
        await _submit(pilot, "second")
        assert len(application.sent) == 1  # no second run: it went into the working one
        assert [m for _, m in application.inputs] == ["second"]
        assert [b.text for b in app.tab.queued] == ["second"]
        app.handle_event(event(EventType.INPUT_RECEIVED, text="second"))
        assert app.tab.queued == []  # the model read it
        await _submit(pilot, "/new")  # a built-in still works: a new tab beside the running one
        assert len(app._tabs) == 2 and app.tab.blank
        gate.set()
        await _settle(pilot)


async def test_unread_queued_messages_return_to_the_prompt_when_the_run_stops() -> None:
    gate = asyncio.Event()
    application = FakeApplication(events=[event(EventType.RUN_STARTED)], gate=gate)
    app = _app(application)

    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "first")
        await _submit(pilot, "second")
        app._finish_run(app.tab, "failed")
        assert app.tab.queued == []
        assert app.query_one("#prompt", PromptArea).text == "second"
        gate.set()
        await _settle(pilot)


# ── cancel / quit ────────────────────────────────────────────────────────────


async def test_escape_returns_focus_to_the_prompt_when_idle() -> None:
    app = _app()

    async with app.run_test(size=(140, 40)) as pilot:
        app.tab.focus()
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
        assert "make the panels sligh..." in _tabs_text(app)  # cut to fit the tab
        assert "panels slightly darker" in str(app.query_one("#sb-session").render())
        assert "working" in str(app.query_one("#sb-activity").render())
        side = str(app.query_one("#sb-context").render())
        assert "200K" in side
        assert "working" in str(app.query_one("#sb-activity").render())


async def test_header_shows_done_after_a_completed_run() -> None:
    app = _app(
        FakeApplication(events=[event(EventType.RUN_STARTED), event(EventType.RUN_COMPLETED)])
    )

    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "hi")
        assert "done" in str(app.query_one("#sb-activity").render())


async def test_welcome_shows_when_empty() -> None:
    app = _app()

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        assert len(app.tab.query(Welcome)) == 1  # the empty state explains the basics


async def test_new_conversation_starts_a_new_session_and_keeps_the_old_one() -> None:
    application = FakeApplication(
        events=[event(EventType.RUN_STARTED), event(EventType.RUN_COMPLETED)]
    )
    app = _app(application)

    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "first task")
        assert len(app.tab.query(Welcome)) == 0
        assert app.tab.tokens > 0
        first = app.tab.session_id
        assert application.titles[first] == "first task"  # persisted so it can be found

        await pilot.press("ctrl+n")
        await _settle(pilot, 3)

        assert app.tab.session_id is None  # a draft: stored only when a message is sent
        assert len(app.tab.query(Welcome)) == 1
        assert len(app.tab.query(UserMessage)) == 0
        assert app.tab.tokens == 0
        assert app.tab.title is None
        assert "New conversation" in _tabs_text(app)

        await _submit(pilot, "second task")
        assert app.tab.session_id not in (None, first)  # a real, separate session
        assert application.titles[app.tab.session_id] == "second task"


async def test_launch_is_a_fresh_draft_and_stores_nothing_until_the_first_message() -> None:
    application = FakeApplication(
        events=[event(EventType.RUN_STARTED), event(EventType.RUN_COMPLETED)]
    )
    app = TuiApp(application)  # type: ignore[arg-type]  # no session_id: a normal launch

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        assert app.tab.session_id is None
        assert application.sessions == []  # opening (and quitting) leaves no empty chat
        assert len(app.tab.query(Welcome)) == 1
        assert "not saved yet" in str(app.query_one("#sb-session").render())

        await _submit(pilot, "hello")
        assert app.tab.session_id is not None
        assert [s.id for s in application.sessions] == [app.tab.session_id]
        assert application.sent[0][0].session == app.tab.session_id


async def test_new_tab_while_a_run_is_active_leaves_it_running() -> None:
    gate = asyncio.Event()
    app = _app(FakeApplication(events=[event(EventType.RUN_STARTED)], gate=gate))

    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "busy")
        working = app.tab
        await pilot.press("ctrl+n")
        await _settle(pilot, 3)
        assert app.tab is not working and app.tab.blank
        assert working.running  # still going in its own tab
        assert len(app._tabs) == 2
        await pilot.press("alt+1")
        await _settle(pilot)
        assert app.tab is working
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
        assert app.tab.session_id == "old-1"
        assert app.tab.title == "Fix the retry bug"
        assert len(app.tab.query(Welcome)) == 0
        assert len(app.tab.query(UserMessage)) == 1
        assert len(app.tab.query(AgentMessage)) == 1
        card = app.tab.query_one(Disclosure)
        assert card.has_body  # arguments are available behind the chevron
        text = _chat_text(app)
        assert "fix the retry bug" in text and "added backoff" in text
        assert "Fix the retry bug" in _tabs_text(app)


async def test_picker_escape_keeps_the_current_conversation() -> None:
    application = FakeApplication()
    application.sessions = [SessionSummary(id="old-1", title="Earlier")]
    app = _app(application)

    async with app.run_test(size=(140, 40)) as pilot:
        current = app.tab.session_id
        await pilot.press("ctrl+o")
        await _settle(pilot, 4)
        await pilot.press("escape")
        await _settle(pilot, 3)
        assert not isinstance(app.screen, SessionPicker)
        assert app.tab.session_id == current


async def test_picker_with_no_conversations_says_so() -> None:
    app = _app()

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("ctrl+o")
        await _settle(pilot, 4)
        assert isinstance(app.screen, SessionPicker)
        await pilot.press("escape")


# ── responsive layout ────────────────────────────────────────────────────────


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


# ── deleting conversations from the picker ───────────────────────────────────
def _with_sessions() -> FakeApplication:
    application = FakeApplication()
    application.sessions = [
        SessionSummary(id="keep", title="Keep me"),
        SessionSummary(id="gone", title="Delete me"),
    ]
    return application


async def test_picker_delete_needs_a_second_press() -> None:
    application = _with_sessions()
    app = _app(application)

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("ctrl+o")
        await _settle(pilot, 4)
        await pilot.press("down", "d")  # highlight "Delete me", ask once
        await _settle(pilot, 3)
        assert application.deleted == []
        assert "again" in str(app.screen.query_one("#picker-foot").render())

        await pilot.press("d")  # confirm
        await _settle(pilot, 4)
        assert application.deleted == ["gone"]
        assert app.screen.query_one("#picker-list").option_count == 1


async def test_picker_delete_is_cancelled_by_moving_the_highlight() -> None:
    application = _with_sessions()
    app = _app(application)

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("ctrl+o")
        await _settle(pilot, 4)
        await pilot.press("down", "d", "up", "d")  # the second d targets a different row
        await _settle(pilot, 4)
        assert application.deleted == []


async def test_deleting_the_open_conversation_returns_to_a_fresh_draft() -> None:
    application = _with_sessions()
    application.histories["keep"] = [HistoryItem("user", "hi"), HistoryItem("assistant", "yo")]
    app = _app(application)

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("ctrl+o")
        await _settle(pilot, 4)
        await pilot.press("enter")  # open "Keep me"
        await _settle(pilot, 8)
        assert app.tab.session_id == "keep"
        assert len(app.tab.query(UserMessage)) == 1

        await pilot.press("ctrl+o")
        await _settle(pilot, 4)
        await pilot.press("d", "d")  # the highlighted row is the current conversation
        await _settle(pilot, 6)
        assert application.deleted == ["keep"]
        assert app.tab.session_id is None
        await pilot.press("escape")
        await _settle(pilot, 4)
        assert len(app.tab.query(UserMessage)) == 0
        assert len(app.tab.query(Welcome)) == 1


# ── approvals ────────────────────────────────────────────────────────────────
def _approval_stream(gate: asyncio.Event) -> FakeApplication:
    return FakeApplication(
        events=[
            event(EventType.RUN_STARTED),
            event(
                EventType.TOOL_STARTED,
                tool="run_command",
                tool_call_id="c1",
                args={"command": "rm -rf build"},
            ),
            event(
                EventType.APPROVAL_REQUIRED,
                approval_id="a1",
                tool="run_command",
                tool_call_id="c1",
                args={"command": "rm -rf build"},
            ),
        ],
        gate=gate,
    )


async def test_an_approval_request_opens_a_dialog_and_y_approves() -> None:
    gate = asyncio.Event()
    application = _approval_stream(gate)
    app = _app(application)

    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "clean up")
        await _settle(pilot, 8)

        assert isinstance(app.screen, ApprovalScreen)
        assert "rm -rf build" in str(app.screen.query_one("#approval-detail").render())
        assert app.tab.cards["c1"].status == "waiting"
        assert "approval" in str(app.query_one("#sb-activity").render())

        await pilot.press("y")
        await _settle(pilot, 4)

        ((approval_id, run_id, session_id, principal, approved),) = application.decisions
        assert (approval_id, approved) == ("a1", True)
        assert run_id == application.sent[0][1]
        assert session_id == "s1" and principal == app._principal
        assert not isinstance(app.screen, ApprovalScreen)
        gate.set()
        await _settle(pilot)


async def test_n_and_escape_reject_but_enter_does_not_approve() -> None:
    gate = asyncio.Event()
    application = _approval_stream(gate)
    app = _app(application)

    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "clean up")
        await _settle(pilot, 8)

        await pilot.press("enter")  # a stray Enter must not approve a command
        await _settle(pilot, 3)
        assert application.decisions == []
        assert isinstance(app.screen, ApprovalScreen)

        await pilot.press("escape")
        await _settle(pilot, 4)
        assert [d[-1] for d in application.decisions] == [False]
        gate.set()
        await _settle(pilot)


async def test_approval_resolved_resumes_or_leaves_the_card_to_fail() -> None:
    app = _app()

    async with app.run_test(size=(140, 40)) as pilot:
        app.handle_event(
            event(
                EventType.TOOL_STARTED, tool="run_command", tool_call_id="c", args={"command": "x"}
            )
        )
        card = app.tab.cards["c"]
        card.await_approval()
        app.tab.awaiting = 1
        app.handle_event(
            event(EventType.APPROVAL_RESOLVED, approval_id="a", tool_call_id="c", status="approved")
        )
        await _settle(pilot, 3)
        assert card.status == "running"
        assert app.tab.awaiting == 0


async def test_a_stream_that_ends_without_a_terminal_event_returns_the_ui_to_idle() -> None:
    """A hard cancel publishes no run.failed; the UI must not stay on 'cancelling'."""
    application = FakeApplication(events=[event(EventType.RUN_STARTED)])  # stream just ends
    app = _app(application)

    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "go")
        await _settle(pilot, 6)

        assert not app.tab.running
        assert app.tab.outcome == "failed"
        assert "failed" in str(app.query_one("#sb-activity").render())
        await _submit(pilot, "second")  # and the next message is accepted
        assert len(application.sent) == 2


async def test_a_card_left_waiting_when_the_run_ends_is_marked_failed() -> None:
    """Regression: a cancelled run left its card on 'awaiting approval' forever."""
    gate = asyncio.Event()
    app = _app(_approval_stream(gate))

    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "clean up")
        await _settle(pilot, 8)
        assert app.tab.cards["c1"].status == "waiting"

        app.tab.cancel_sent = True
        app._finish_run(app.tab, "failed")  # what _consume does when the stream ends after a cancel
        await _settle(pilot, 4)

        assert app.tab.cards["c1"].status == "failed"
        assert not isinstance(app.screen, ApprovalScreen)  # the dialog closed too
        gate.set()
        await _settle(pilot)


async def test_a_failing_delete_is_reported_not_raised() -> None:
    application = _with_sessions()

    async def broken(session_id: str) -> None:
        raise OSError("disk full")

    application.delete_session = broken  # type: ignore[method-assign]
    app = _app(application)

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("ctrl+o")
        await _settle(pilot, 4)
        await pilot.press("d", "d")
        await _settle(pilot, 4)

        assert isinstance(app.screen, SessionPicker)  # still open, app still alive
        assert app.screen.query_one("#picker-list").option_count == 2


# ── consolidated: cancel keys, sidebar visibility, theme, quit ───────────────
@pytest.mark.parametrize("key", ["escape", "ctrl+c"])
async def test_cancel_keys_cancel_a_live_run(key: str) -> None:
    gate = asyncio.Event()
    application = FakeApplication(events=[event(EventType.RUN_STARTED)], gate=gate)
    app = _app(application)

    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "long task")
        run_id = application.sent[0][1]
        assert app.tab.run_id == run_id

        await pilot.press(key)
        await pilot.pause()

        assert application.cancelled == [run_id]
        assert app.tab.cancel_sent
        gate.set()
        await pilot.pause()


async def test_a_second_ctrl_c_quits_while_cancelling() -> None:
    gate = asyncio.Event()
    app = _app(FakeApplication(events=[event(EventType.RUN_STARTED)], gate=gate))

    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "long task")
        await pilot.press("ctrl+c")  # cancel
        await pilot.pause()
        await pilot.press("ctrl+c")  # the run is still winding down: now quit
        await _settle(pilot, 3)

        assert not app.is_running


async def test_ctrl_q_quits() -> None:
    """Quit is Textual's own binding now (ours duplicated it)."""
    app = _app()

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("ctrl+q")
        await _settle(pilot, 3)

        assert not app.is_running


@pytest.mark.parametrize(
    ("size", "presses", "visible"),
    [
        ((140, 40), (), True),  # wide: shown by default
        ((88, 24), (), False),  # narrow: hidden by default
        ((88, 24), ("ctrl+b",), True),  # forced on at a narrow width
        ((88, 24), ("ctrl+b", "ctrl+b"), False),  # a second press returns to automatic
        ((140, 40), ("ctrl+b",), False),  # hidden on a wide terminal
    ],
)
async def test_sidebar_visibility(
    size: tuple[int, int], presses: tuple[str, ...], visible: bool
) -> None:
    app = _app()

    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        if presses:
            await pilot.press(*presses)
            await pilot.pause()

        assert (app.query_one("#sidebar").styles.display != "none") is visible


async def test_sidebar_follows_the_width_as_the_terminal_is_resized() -> None:
    app = _app()

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        sidebar = app.query_one("#sidebar")
        await pilot.resize_terminal(88, 24)
        await pilot.pause()
        assert sidebar.styles.display == "none"
        await pilot.resize_terminal(140, 40)
        await pilot.pause()
        assert sidebar.styles.display != "none"


async def test_default_theme_is_dark_and_ctrl_t_toggles_dark_and_light() -> None:
    app = _app()  # the fake config now carries the real default theme

    async with app.run_test(size=(140, 40)) as pilot:
        assert app.theme == "agent-stack-dark" and app.current_theme.dark
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert app.theme == "agent-stack-light"
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert app.theme == "agent-stack-dark"


# ── cost ─────────────────────────────────────────────────────────────────────
from agent_stack.gateways.base import SessionUsage  # noqa: E402


def _spent_text(app: TuiApp) -> str:
    return str(app.query_one("#sb-activity").render())


async def test_tui_shows_the_conversation_cost_live_and_reconciles_it_after_the_run() -> None:
    application = FakeApplication(
        events=[
            event(EventType.RUN_STARTED),
            event(
                EventType.MODEL_USAGE,
                input_tokens=1200,
                output_tokens=80,
                cost_usd=0.002,
                model="m",
            ),
            event(EventType.ASSISTANT_MESSAGE, content="hi there"),
            event(EventType.RUN_COMPLETED, steps=1, tool_calls=0),
        ]
    )
    # the durable total also holds a subagent's calls that never reached this stream
    application.usages["s1"] = [
        SessionUsage(calls=3, priced_calls=3, input_tokens=5000, output_tokens=300, cost_usd=0.0071)
    ]
    app = _app(application)

    async with app.run_test(size=(140, 40)) as pilot:
        assert "spent" not in _spent_text(app)  # nothing spent yet: no cost shown
        await _submit(pilot, "hello")
        await pilot.pause(0.3)
        sidebar = _spent_text(app)

    assert "$0.0071 · 5k in · 300 out · 3 calls" in sidebar  # reconciled from the durable log
    assert "spent" in sidebar


async def test_tui_a_live_usage_event_updates_the_total_before_the_run_ends() -> None:
    gate = asyncio.Event()
    application = FakeApplication(
        events=[
            event(EventType.RUN_STARTED),
            event(EventType.MODEL_USAGE, input_tokens=1000, output_tokens=100, cost_usd=0.004),
        ],
        gate=gate,
    )
    app = _app(application)

    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "go")
        await pilot.pause(0.2)
        live = _spent_text(app)
        gate.set()

    assert "$0.0040 · 1k in · 100 out · 1 calls" in live


# ── diffs of what the agent wrote or edited ──────────────────────────────────
from agent_stack.app.application import FileDiff  # noqa: E402
from agent_stack.gateways.tui.screens import ChangesScreen  # noqa: E402

_EDIT_DIFF = "--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,2 @@\n x = 1\n-y = 2\n+y = 3"


def _file_diff(
    path: str = "a.py", diff: str = _EDIT_DIFF, kind: str = "modified", edits: int = 1
) -> FileDiff:
    return FileDiff(
        path=path,
        kind=kind,  # type: ignore[arg-type]
        edits=edits,
        added=diff.count("\n+") - diff.count("\n+++"),
        removed=diff.count("\n-") - diff.count("\n---"),
        diff=diff,
        before="x = 1\ny = 2\n",
        after="x = 1\ny = 3\n",
    )


def _file_tool_events(tool: str = "edit_file", ok: bool = True) -> list[Any]:
    args = {"path": "a.py", "old": "y = 2", "new": "y = 3"}
    return [
        event(EventType.RUN_STARTED),
        event(EventType.TOOL_STARTED, tool=tool, tool_call_id="c1", args=args),
        event(EventType.TOOL_COMPLETED, tool=tool, tool_call_id="c1", ok=ok),
        event(EventType.RUN_COMPLETED, steps=1, tool_calls=1),
    ]


def _card_text(card: Disclosure) -> str:
    return "\n".join(card._lines)


async def test_an_edit_card_opens_on_the_diff_of_what_that_call_changed() -> None:
    application = FakeApplication(events=_file_tool_events())
    application.latest_diffs[("s1", "a.py")] = _file_diff()
    app = _app(application)

    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "fix y")
        await pilot.pause(0.3)
        (card,) = app.tab.query(Disclosure)
        assert card.status == "done" and card.diff is True
        assert card._is_open and card.has_body
        text = _card_text(card)

    assert "-y = 2" in text and "+y = 3" in text
    assert "old:" not in text and "new:" not in text  # the arguments gave way to the diff
    assert ("s1", "a.py", True) in application.diff_lookups  # asked for just this call's edit


async def test_a_failed_edit_shows_no_diff_and_a_read_never_asks_for_one() -> None:
    application = FakeApplication(events=_file_tool_events(ok=False))
    application.latest_diffs[("s1", "a.py")] = _file_diff()
    app = _app(application)
    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "try")
        await pilot.pause(0.3)
        (card,) = app.tab.query(Disclosure)
        assert card.status == "failed" and "+y = 3" not in _card_text(card)
    assert application.diff_lookups == []

    reading = FakeApplication(events=_file_tool_events("read_file"))
    app = _app(reading)
    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "read")
        await pilot.pause(0.3)
        (card,) = app.tab.query(Disclosure)
        assert card.status == "done"
    assert reading.diff_lookups == []


async def test_a_diff_that_cannot_be_fetched_never_leaves_the_card_spinning() -> None:
    application = FakeApplication(events=_file_tool_events())

    async def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("db is down")

    application.changed_file = broken  # type: ignore[method-assign]
    app = _app(application)

    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "go")
        await pilot.pause(0.3)
        (card,) = app.tab.query(Disclosure)
        assert card.status == "done"


async def test_ctrl_g_lists_the_changed_files_and_shows_each_diff() -> None:
    application = FakeApplication()
    new_diff = "--- /dev/null\n+++ b/n.md\n@@ -0,0 +1,2 @@\n+# Title\n+body"
    application.file_diffs["s1"] = [_file_diff(), _file_diff("n.md", new_diff, "created", 2)]
    app = _app(application)

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("ctrl+g")
        await pilot.pause(0.2)
        screen = app.screen
        assert isinstance(screen, ChangesScreen)
        listing = screen.query_one("#changes-list")
        assert listing.option_count == 2  # type: ignore[attr-defined]
        first = str(screen.query_one("#changes-diff").render())
        await pilot.press("down")
        await pilot.pause(0.2)
        second = str(screen.query_one("#changes-diff").render())
        await pilot.press("escape")
        await pilot.pause(0.2)
        closed = not isinstance(app.screen, ChangesScreen)

    assert "-y = 2" in first and "+y = 3" in first
    assert "+# Title" in second and "--- /dev/null" in second
    assert closed


async def test_ctrl_g_with_nothing_changed_says_so_and_without_a_session_does_not_open() -> None:
    app = _app(FakeApplication())
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("ctrl+g")
        await pilot.pause(0.2)
        assert isinstance(app.screen, ChangesScreen)
        assert "has not changed any files" in " ".join(
            str(w.render()) for w in app.screen.query("Static")
        )

    fresh = TuiApp(FakeApplication())  # type: ignore[arg-type]  # a draft: no session yet
    async with fresh.run_test(size=(140, 40)) as pilot:
        await pilot.press("ctrl+g")
        await pilot.pause(0.2)
        assert not isinstance(fresh.screen, ChangesScreen)


# ── tabs, queued edits, titles, context ──────────────────────────────────────
async def test_up_arrow_takes_back_the_last_queued_message_to_edit() -> None:
    gate = asyncio.Event()
    application = FakeApplication(events=[event(EventType.RUN_STARTED)], gate=gate)
    app = _app(application)

    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "first")
        await _submit(pilot, "also do x")
        assert [b.text for b in app.tab.queued] == ["also do x"]
        await pilot.press("up")
        await _settle(pilot)
        assert application.withdrawn == [("run-1", "also do x")]
        assert app.tab.queued == []
        assert len(app.tab.query(UserMessage)) == 1  # the queued bubble is gone
        assert app.query_one("#prompt", PromptArea).text == "also do x"

        application.withdraw_ok = False  # too late: the model has read it
        await _submit(pilot, "and y")
        await pilot.press("up")
        await _settle(pilot)
        assert [b.text for b in app.tab.queued] == ["and y"]
        assert app.query_one("#prompt", PromptArea).text == ""
        gate.set()
        await _settle(pilot)


async def test_a_background_tab_keeps_its_own_conversation() -> None:
    gate = asyncio.Event()
    app = _app(FakeApplication(events=[event(EventType.RUN_STARTED)], gate=gate))

    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "long job")
        first = app.tab
        await pilot.press("ctrl+n")
        await _settle(pilot, 3)
        second = app.tab

        app.handle_event(event(EventType.ASSISTANT_MESSAGE, content="finished it"), first)
        app.handle_event(event(EventType.RUN_COMPLETED), first)
        await _settle(pilot)

        assert app.tab is second  # nothing pulled the view away
        assert len(first.query(AgentMessage)) == 1 and len(second.query(AgentMessage)) == 0
        assert first.outcome == "done" and not first.running
        gate.set()
        await _settle(pilot)


async def test_close_tab_keeps_the_other_and_refuses_a_running_one() -> None:
    gate = asyncio.Event()
    app = _app(FakeApplication(events=[event(EventType.RUN_STARTED)], gate=gate))

    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "busy")
        working = app.tab
        await _submit(pilot, "/close")
        assert app._tabs == [working]  # a running tab is not closed
        await pilot.press("ctrl+n")
        await _settle(pilot, 3)
        await _submit(pilot, "/close")
        await _settle(pilot, 3)
        assert app._tabs == [working] and app.tab is working
        gate.set()
        await _settle(pilot)


class _TitlingApplication(FakeApplication):
    async def get_session(self, session_id: str) -> Any:
        return SimpleNamespace(id=session_id, title="Darker Panels")


async def test_the_small_models_title_replaces_the_first_message() -> None:
    application = _TitlingApplication(
        events=[event(EventType.RUN_STARTED), event(EventType.RUN_COMPLETED)]
    )
    app = _app(application)

    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "make the panels slightly darker please")
        await _settle(pilot)
        assert app.tab.title == "Darker Panels"
        assert "Darker Panels" in _tabs_text(app)


async def test_context_is_the_size_the_model_reported_for_the_main_run() -> None:
    gate = asyncio.Event()
    app = _app(FakeApplication(events=[event(EventType.RUN_STARTED)], gate=gate))

    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "hi")
        assert "~" in str(app.query_one("#sb-context").render())  # an estimate so far
        app.handle_event(event(EventType.MODEL_USAGE, run_id="run-1", input_tokens=35_800))
        app.handle_event(
            event(EventType.MODEL_USAGE, run_id="run-1", input_tokens=90, purpose="title")
        )
        app.handle_event(
            event(EventType.MODEL_USAGE, run_id="child", parent_run_id="run-1", input_tokens=5)
        )
        await _settle(pilot)
        side = str(app.query_one("#sb-context").render())
        assert "35.8K" in side and "~" not in side
        assert "context" not in str(app.query_one("#sb-activity").render())  # shown once
        gate.set()
        await _settle(pilot)
