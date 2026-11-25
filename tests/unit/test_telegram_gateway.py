"""P25: the Telegram gateway: an adapter over a scripted fake Bot API and a fake Application."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any

import httpx
import pytest

from agent_stack.core.events import EventType
from agent_stack.gateways.base import HistoryItem, SessionSummary
from agent_stack.gateways.telegram import (
    TelegramApi,
    TelegramConfigError,
    TelegramError,
    TelegramGateway,
    TelegramSettings,
    split_message,
)
from agent_stack.observability.redact import redact
from tests.unit.gateway_fakes import FakeApplication, event

OWNER = 7
TOKEN = "123456789:AAEhBP0av28VxbRnGAcBpFAKEexampleTOKEN_x"


class FakeApi:
    """Records what the gateway sends; the tests drive updates in by hand."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str, dict[str, Any] | None]] = []
        self.edits: list[tuple[int, int, str]] = []
        self.answers: list[tuple[str, str]] = []
        self.actions: list[int] = []
        self._next_id = 100
        self.updates: list[list[dict[str, Any]]] = []
        self.poll_errors: list[TelegramError] = []
        self.offsets: list[int] = []
        self.modes: list[str | None] = []
        self.edit_markups: list[Any] = []
        self.commands: list[tuple[str, str]] = []
        self.reject_html = False

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = None,
    ) -> int:
        if parse_mode == "HTML" and self.reject_html:
            raise TelegramError("can't parse entities", 400)
        self._next_id += 1
        self.sent.append((chat_id, text, reply_markup))
        self.modes.append(parse_mode)
        return self._next_id

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: Any = None,
        parse_mode: str | None = None,
    ) -> None:
        self.edits.append((chat_id, message_id, text))
        self.edit_markups.append(reply_markup)

    async def set_my_commands(self, commands: list[tuple[str, str]]) -> None:
        self.commands = list(commands)

    async def answer_callback_query(self, callback_id: str, text: str = "") -> None:
        self.answers.append((callback_id, text))

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        self.actions.append(chat_id)

    async def get_updates(self, offset: int, poll_seconds: int = 30) -> list[dict[str, Any]]:
        await asyncio.sleep(0)  # a real poll does network I/O and yields to other tasks
        self.offsets.append(offset)
        if self.poll_errors:
            raise self.poll_errors.pop(0)
        return self.updates.pop(0) if self.updates else []

    def texts(self) -> list[str]:
        return [text for _, text, _ in self.sent]


def _settings(**kwargs: Any) -> TelegramSettings:
    return TelegramSettings(token=TOKEN, allowed_users=frozenset({OWNER}), **kwargs)


def _gateway(
    application: FakeApplication, api: FakeApi | None = None, **kwargs: Any
) -> tuple[TelegramGateway, FakeApi]:
    api = api or FakeApi()
    settings = kwargs.pop("settings", _settings())
    return TelegramGateway(application, api, settings, edit_interval=0.0, **kwargs), api  # type: ignore[arg-type]


_UPDATE_IDS = iter(range(1, 10_000))


def _message(
    text: str, *, user: int = OWNER, chat: int | None = None, message_id: int | None = None
) -> dict[str, Any]:
    update_id = next(_UPDATE_IDS)
    chat = user if chat is None else chat  # in a private chat the chat id is the user's id
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id or update_id,
            "from": {"id": user},
            "chat": {"id": chat, "type": "private"},
            "text": text,
        },
    }


def _callback(data: str, *, user: int = OWNER, chat: int | None = None) -> dict[str, Any]:
    chat = user if chat is None else chat
    return {
        "update_id": next(_UPDATE_IDS),
        "callback_query": {
            "id": f"cb-{data}",
            "from": {"id": user},
            "data": data,
            "message": {"chat": {"id": chat, "type": "private"}, "message_id": 1},
        },
    }


async def _settle(gateway: TelegramGateway, chat: int = OWNER) -> None:
    """Wait for the chat's run task (if any) to finish."""
    state = gateway._chats.get(chat)
    if state is not None and state.task is not None:
        await asyncio.wait_for(state.task, 2.0)


async def _say(gateway: TelegramGateway, text: str, **kwargs: Any) -> None:
    await gateway.handle_update(_message(text, **kwargs))
    await _settle(gateway, kwargs.get("chat", OWNER))


def _reply_events(text: str = "all done") -> list[Any]:
    return [
        event(EventType.RUN_STARTED),
        event(EventType.ASSISTANT_MESSAGE, content=text),
        event(EventType.RUN_COMPLETED, steps=1, tool_calls=0),
    ]


# ── settings: fail closed ────────────────────────────────────────────────────
def test_settings_need_a_token_and_an_explicit_allow_list() -> None:
    with pytest.raises(TelegramConfigError, match="TELEGRAM_BOT_TOKEN"):
        TelegramSettings.from_env({})
    with pytest.raises(TelegramConfigError, match="allow-list"):
        TelegramSettings.from_env({"TELEGRAM_BOT_TOKEN": TOKEN})
    with pytest.raises(TelegramConfigError, match="numeric"):
        TelegramSettings.from_env({"TELEGRAM_BOT_TOKEN": TOKEN, "AGENT_STACK_TELEGRAM_USERS": "me"})


def test_settings_parse_users_and_never_print_the_token() -> None:
    settings = TelegramSettings.from_env(
        {
            "TELEGRAM_BOT_TOKEN": TOKEN,
            "AGENT_STACK_TELEGRAM_USERS": "7, 9",
            "AGENT_STACK_TELEGRAM_PRINCIPAL": "tui:local-user",
        }
    )

    assert settings.allowed_users == {7, 9} and settings.principal == "tui:local-user"
    assert TOKEN not in repr(settings)


def test_redact_knows_the_bot_token_shape() -> None:
    assert TOKEN not in redact(f"GET /bot{TOKEN}/getMe failed")


# ── splitting ────────────────────────────────────────────────────────────────
def test_split_message_respects_the_limit_and_keeps_all_content() -> None:
    assert split_message("short") == ["short"]
    text = "\n".join(f"line {i} " + "x" * 40 for i in range(200))
    chunks = split_message(text, 500)
    assert all(0 < len(c) <= 500 for c in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_split_message_hard_cuts_when_there_is_no_newline() -> None:
    chunks = split_message("a" * 1000, 400)

    assert [len(c) for c in chunks] == [400, 400, 200]


# ── who is served ────────────────────────────────────────────────────────────
async def test_strangers_are_ignored_without_a_reply() -> None:
    application = FakeApplication(events=_reply_events())
    gateway, api = _gateway(application)

    await _say(gateway, "run rm -rf /", user=999)
    await gateway.handle_update(_callback("ap:abc:y", user=999))

    assert api.sent == [] and api.answers == [] and application.sent == []


# ── a message becomes a run ──────────────────────────────────────────────────
async def test_a_message_starts_a_run_in_a_new_titled_session() -> None:
    application = FakeApplication(events=_reply_events("here you go"))
    gateway, api = _gateway(application)

    await _say(gateway, "fix the retry bug", message_id=42)

    ((envelope, run_id, _),) = application.sent
    assert envelope.text == "fix the retry bug"
    assert envelope.principal == f"telegram:{OWNER}"
    assert envelope.session == f"telegram:{OWNER}-new1"  # created on the first message
    assert envelope.metadata == {"idempotency_key": f"message:{OWNER}:42"}
    assert application.titles[envelope.session] == "fix the retry bug"
    assert "here you go" in api.texts()
    assert api.actions == [OWNER]  # a typing indicator
    assert api.edits[-1][2].startswith("✓ done")
    assert run_id == "run-1"


async def test_the_next_message_continues_the_same_session() -> None:
    application = FakeApplication(events=_reply_events())
    gateway, _ = _gateway(application)

    await _say(gateway, "first")
    await _say(gateway, "second")

    assert application.sent[0][0].session == application.sent[1][0].session
    assert len(application.sessions) == 1


async def test_a_configured_principal_shares_the_tuis_identity() -> None:
    application = FakeApplication(events=_reply_events())
    gateway, _ = _gateway(application, settings=_settings(principal="tui:local-user"))

    await _say(gateway, "hello")

    assert application.sent[0][0].principal == "tui:local-user"


async def test_a_redelivered_update_is_acted_on_once() -> None:
    application = FakeApplication(events=_reply_events())
    gateway, _ = _gateway(application)
    update = _message("do it once", message_id=5)

    await gateway.handle_update(update)
    await _settle(gateway)
    await gateway.handle_update(update)  # same update_id: Telegram redelivered it
    await gateway.handle_update({**update, "update_id": 99_999})  # same message, new update
    await _settle(gateway)

    assert len(application.sent) == 1


async def test_a_second_message_during_a_run_is_refused() -> None:
    gate = asyncio.Event()
    application = FakeApplication(events=[event(EventType.RUN_STARTED)], gate=gate)
    gateway, api = _gateway(application)

    await gateway.handle_update(_message("long task"))
    await asyncio.sleep(0.05)
    await gateway.handle_update(_message("and another"))
    await gateway.handle_update(_message("/new"))

    assert len(application.sent) == 1
    assert sum("in progress" in t for t in api.texts()) == 2
    gate.set()
    await _settle(gateway)


# ── commands ─────────────────────────────────────────────────────────────────
async def test_new_starts_a_fresh_draft_and_start_shows_help() -> None:
    application = FakeApplication(events=_reply_events())
    gateway, api = _gateway(application)
    await _say(gateway, "first")
    first = application.sent[0][0].session

    await _say(gateway, "/new")
    await _say(gateway, "second")
    await _say(gateway, "/start")

    assert application.sent[1][0].session != first
    assert any("/sessions" in t for t in api.texts())


async def test_sessions_lists_conversations_and_a_tap_switches_with_a_recap() -> None:
    application = FakeApplication(events=_reply_events())
    application.sessions = [
        SessionSummary(id="tui-1", title="Fix the retry bug"),
        SessionSummary(id="tui-2", title="Other"),
    ]
    application.histories["tui-1"] = [
        HistoryItem("user", "fix the retry bug"),
        HistoryItem("tool", "read_file", {"path": "x"}),
        HistoryItem("assistant", "Added backoff."),
    ]
    gateway, api = _gateway(application)

    await _say(gateway, "/sessions")
    _, _, keyboard = api.sent[-1]
    buttons = [row[0] for row in keyboard["inline_keyboard"]]
    assert [b["callback_data"] for b in buttons] == ["sw:0", "sw:1"]
    assert "Fix the retry bug" in buttons[0]["text"]

    await gateway.handle_update(_callback("sw:0"))

    assert api.answers[-1][1] == "Switched"
    assert "Switched to “Fix the retry bug”" in api.sent[-1][1]
    assert (
        "You: fix the retry bug" in api.sent[-1][1] and "Agent: Added backoff." in api.sent[-1][1]
    )


async def test_a_conversation_started_elsewhere_continues_here() -> None:
    """Acceptance: with a shared principal, a TUI session is continued from Telegram."""
    application = FakeApplication(events=_reply_events("continuing"))
    application.sessions = [SessionSummary(id="tui:local-user-ab12cd34", title="From the TUI")]
    gateway, _ = _gateway(application, settings=_settings(principal="tui:local-user"))

    await _say(gateway, "/sessions")
    await gateway.handle_update(_callback("sw:0"))
    await _say(gateway, "carry on")

    envelope = application.sent[0][0]
    assert (envelope.session, envelope.principal) == (
        "tui:local-user-ab12cd34",
        "tui:local-user",
    )
    assert application.sessions[0].id == "tui:local-user-ab12cd34"  # no new session was made


async def test_a_stale_switch_button_is_rejected_politely() -> None:
    gateway, api = _gateway(FakeApplication())

    await gateway.handle_update(_callback("sw:5"))

    assert "out of date" in api.answers[-1][1]


async def test_empty_session_list_says_so() -> None:
    gateway, api = _gateway(FakeApplication())

    await _say(gateway, "/sessions")

    assert api.texts() == ["No conversations yet."]


async def test_cancel_stops_the_live_run_and_is_a_noop_when_idle() -> None:
    gate = asyncio.Event()
    application = FakeApplication(events=[event(EventType.RUN_STARTED)], gate=gate)
    gateway, api = _gateway(application)

    await _say(gateway, "/cancel")
    assert api.texts() == ["Nothing is running."]

    await gateway.handle_update(_message("long task"))
    await asyncio.sleep(0.05)
    await gateway.handle_update(_message("/cancel"))  # (not _say: the fake run ends on the gate)

    assert application.cancelled == ["run-1"]
    gate.set()
    await _settle(gateway)


# ── approvals ────────────────────────────────────────────────────────────────
def _approval_events() -> list[Any]:
    return [
        event(EventType.RUN_STARTED),
        event(
            EventType.TOOL_STARTED,
            tool="run_command",
            tool_call_id="c1",
            args={"command": "rm -rf build"},
        ),
        event(
            EventType.APPROVAL_REQUIRED,
            approval_id="a1b2c3",
            tool="run_command",
            tool_call_id="c1",
            args={"command": "rm -rf build"},
        ),
    ]


async def _start_pending_approval() -> tuple[
    TelegramGateway, FakeApi, FakeApplication, asyncio.Event
]:
    gate = asyncio.Event()
    application = FakeApplication(events=_approval_events(), gate=gate)
    gateway, api = _gateway(application)
    await gateway.handle_update(_message("clean up"))
    await asyncio.sleep(0.05)
    return gateway, api, application, gate


async def test_a_gated_call_asks_with_an_inline_keyboard_and_approve_answers_it() -> None:
    gateway, api, application, gate = await _start_pending_approval()

    _, text, keyboard = api.sent[-1]
    assert "rm -rf build" in text
    data = [b["callback_data"] for b in keyboard["inline_keyboard"][0]]
    assert data == ["ap:a1b2c3:y", "ap:a1b2c3:n"]

    await gateway.handle_update(_callback("ap:a1b2c3:y"))

    ((approval_id, run_id, session_id, principal, approved),) = application.decisions
    assert (approval_id, approved) == ("a1b2c3", True)
    assert run_id == "run-1" and session_id == "s1" and principal == f"telegram:{OWNER}"
    assert api.answers[-1][1] == "Approved"
    assert api.edits[-1][2] == "✓ Approved: run_command rm -rf build"
    gate.set()
    await _settle(gateway)


async def test_reject_and_stale_and_stranger_callbacks() -> None:
    gateway, api, application, gate = await _start_pending_approval()

    await gateway.handle_update(_callback("ap:a1b2c3:y", user=999))  # a stranger: ignored
    assert application.decisions == [] and api.answers == []

    await gateway.handle_update(_callback("ap:a1b2c3:n"))
    assert [d[-1] for d in application.decisions] == [False]
    assert api.edits[-1][2].startswith("✗ Rejected")

    await gateway.handle_update(_callback("ap:a1b2c3:y"))  # already answered
    assert len(application.decisions) == 1
    assert "no longer pending" in api.answers[-1][1]
    gate.set()
    await _settle(gateway)


async def test_a_refused_decision_is_reported_to_the_user() -> None:
    gateway, api, application, gate = await _start_pending_approval()

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("approval already cancelled")

    application.decide_approval = refuse  # type: ignore[method-assign]
    await gateway.handle_update(_callback("ap:a1b2c3:y"))

    assert "already cancelled" in api.answers[-1][1]
    gate.set()
    await _settle(gateway)


# ── status rendering ─────────────────────────────────────────────────────────
def _tool_events(n: int) -> list[Any]:
    out: list[Any] = [event(EventType.RUN_STARTED)]
    for i in range(n):
        out.append(
            event(
                EventType.TOOL_STARTED,
                tool="read_file",
                tool_call_id=f"c{i}",
                args={"path": f"f{i}.py"},
            )
        )
        out.append(event(EventType.TOOL_COMPLETED, tool="read_file", tool_call_id=f"c{i}", ok=True))
    out.append(event(EventType.RUN_COMPLETED, steps=n, tool_calls=n))
    return out


async def test_status_edits_are_debounced() -> None:
    application = FakeApplication(events=_tool_events(10))
    api = FakeApi()
    now = [0.0]
    gateway = TelegramGateway(
        application,  # type: ignore[arg-type]
        api,  # type: ignore[arg-type]
        _settings(),
        edit_interval=5.0,
        clock=lambda: now[0],
    )

    await gateway.handle_update(_message("read everything"))
    await _settle(gateway)

    # Ten tool calls inside one interval produce no live edit; only the final one lands.
    assert [e[2].split("\n")[0] for e in api.edits] == ["✓ done"]
    assert "→ read_file f9.py" in api.edits[-1][2]


async def test_status_edits_follow_events_when_the_interval_has_passed() -> None:
    application = FakeApplication(events=_tool_events(3))
    gateway, api = _gateway(application)  # interval 0: every event may edit

    await _say(gateway, "read a few")

    live = [e for e in api.edits if e[2].startswith("⏳")]
    assert len(live) == 3
    assert len(api.edits[-1][2].splitlines()) <= 7  # header + at most six lines


async def test_a_failed_tool_shows_on_the_status_line() -> None:
    application = FakeApplication(
        events=[
            event(EventType.RUN_STARTED),
            event(
                EventType.TOOL_STARTED, tool="run_command", tool_call_id="c", args={"command": "x"}
            ),
            event(EventType.TOOL_COMPLETED, tool="run_command", tool_call_id="c", ok=False),
            event(EventType.RUN_COMPLETED),
        ]
    )
    gateway, api = _gateway(application)

    await _say(gateway, "go")

    assert "✗ run_command failed" in api.edits[-1][2]


async def test_a_failed_run_reports_the_error_and_a_cancelled_run_does_not() -> None:
    failing = FakeApplication(
        events=[
            event(EventType.RUN_STARTED),
            event(EventType.RUN_FAILED, error="RuntimeError('boom')"),
        ]
    )
    gateway, api = _gateway(failing)
    await _say(gateway, "go")
    assert api.edits[-1][2].startswith("✗ failed")
    assert any("boom" in t for t in api.texts())

    cancelled = FakeApplication(
        events=[
            event(EventType.RUN_STARTED),
            event(EventType.RUN_FAILED, error="RunCancelled('x')"),
        ]
    )
    gateway, api = _gateway(cancelled)
    await _say(gateway, "go")
    assert api.edits[-1][2].startswith("■ cancelled")
    assert not any("RunCancelled" in t for t in api.texts())


async def test_a_stream_ending_without_a_terminal_event_reads_as_cancelled() -> None:
    gateway, api = _gateway(FakeApplication(events=[event(EventType.RUN_STARTED)]))

    await _say(gateway, "go")

    assert api.edits[-1][2].startswith("■ cancelled")


async def test_a_long_reply_is_split_into_messages_under_the_limit() -> None:
    long = "\n".join(f"paragraph {i} " + "word " * 60 for i in range(40))
    application = FakeApplication(events=_reply_events(long))
    gateway, api = _gateway(application)

    await _say(gateway, "write a lot")

    replies = [t for t in api.texts() if t.startswith("paragraph")]
    assert len(replies) > 1 and all(len(t) <= 4096 for t in replies)


async def test_an_internal_error_is_reported_and_the_chat_stays_usable() -> None:
    application = FakeApplication(events=_reply_events())

    async def broken(envelope: Any, *, parent_run_id: str | None = None) -> Any:
        raise RuntimeError("model down")

    application.send_message = broken  # type: ignore[method-assign]
    gateway, api = _gateway(application)

    await _say(gateway, "hello")

    assert any("model down" in t for t in api.texts())
    assert gateway._chats[OWNER].busy is False
    assert gateway._chats[OWNER].run_id is None


# ── the polling loop ─────────────────────────────────────────────────────────
async def test_polling_acknowledges_updates_and_backs_off_on_network_errors() -> None:
    application = FakeApplication(events=_reply_events())
    api = FakeApi()
    api.poll_errors = [TelegramError("ConnectError calling getUpdates")]
    first = _message("hello")
    api.updates = [[first], []]
    delays: list[float] = []

    async def no_sleep(seconds: float) -> None:
        delays.append(seconds)

    gateway, _ = _gateway(application, api, sleep=no_sleep)

    async def stop_after_polls() -> None:
        while len(api.offsets) < 3:
            await asyncio.sleep(0.01)
        gateway.stop()

    await asyncio.wait_for(asyncio.gather(gateway.run(), stop_after_polls()), 3.0)
    await _settle(gateway)

    assert delays == [1.0]  # one backoff after the one network error
    assert api.offsets[0] == 0 and api.offsets[2] == first["update_id"] + 1
    assert len(application.sent) == 1


async def test_a_rejected_token_stops_the_gateway() -> None:
    api = FakeApi()
    api.poll_errors = [TelegramError("Unauthorized", code=401)]
    gateway, _ = _gateway(FakeApplication(), api)

    with pytest.raises(TelegramError, match="Unauthorized"):
        await asyncio.wait_for(gateway.run(), 2.0)


# ── the Bot API client ───────────────────────────────────────────────────────
def _client(handler: Any) -> TelegramApi:
    return TelegramApi(
        TOKEN,
        httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.telegram.org"
        ),
    )


async def test_api_posts_json_to_the_token_path_and_omits_none() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 77}})

    message_id = await _client(handler).send_message(5, "hi")

    assert message_id == 77
    assert seen[0].url.path == f"/bot{TOKEN}/sendMessage"
    assert json.loads(seen[0].content) == {"chat_id": 5, "text": "hi"}  # no reply_markup key


async def test_api_get_updates_sends_the_long_poll_parameters() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": []})

    assert await _client(handler).get_updates(41, poll_seconds=20) == []
    assert bodies[0] == {
        "offset": 41,
        "timeout": 20,
        "allowed_updates": ["message", "callback_query"],
    }


async def test_api_unwraps_telegram_errors_with_their_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"ok": False, "error_code": 429, "description": "Too Many Requests"}
        )

    with pytest.raises(TelegramError, match="Too Many Requests") as info:
        await _client(handler).send_message(1, "x")

    assert info.value.code == 429


async def test_api_network_errors_never_leak_the_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}")  # httpx text includes the URL

    with pytest.raises(TelegramError) as info:
        await _client(handler).send_chat_action(1)

    assert TOKEN not in str(info.value) and "ConnectError" in str(info.value)


# ── the command line entry point ─────────────────────────────────────────────
def test_cli_telegram_subcommand_refuses_to_start_unsafely(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from agent_stack.observability.cli import main

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.delenv("AGENT_STACK_TELEGRAM_USERS", raising=False)

    assert main(["telegram"]) == 2
    assert "allow-list" in capsys.readouterr().err


def test_gateway_methods_are_accepted_by_the_fake_signature() -> None:
    """Guards the fakes: the real client must offer every method the gateway calls."""
    used: Sequence[str] = (
        "send_message",
        "edit_message_text",
        "answer_callback_query",
        "send_chat_action",
        "get_updates",
    )
    assert all(callable(getattr(TelegramApi, name)) for name in used)


# ── full-featured bot: rendering, paging, tools viewer, modes, state ─────────
from agent_stack.gateways.telegram.render import escape_fit, format_tool_detail, to_html  # noqa: E402


def test_to_html_escapes_everything_and_keeps_code_and_bold() -> None:
    text = (
        "Use **bold** and `a<b` here.\n```python\nif x < 1 & y:\n    pass\n```\n<script>x</script>"
    )

    out = to_html(text)

    assert "<b>bold</b>" in out and "<code>a&lt;b</code>" in out
    assert '<pre><code class="language-python">if x &lt; 1 &amp; y:\n    pass</code></pre>' in out
    assert "<script>" not in out and "&lt;script&gt;" in out


def test_to_html_leaves_unbalanced_markers_literal() -> None:
    assert to_html("a ** b ` c <x>") == "a ** b ` c &lt;x&gt;"


def test_escape_fit_bounds_the_escaped_length() -> None:
    assert len(escape_fit("<&>" * 5000, 1000)) <= 1000
    assert escape_fit("plain", 100) == "plain"


def test_tool_detail_shows_arguments_and_result_within_one_message() -> None:
    item = HistoryItem("tool", "read_file", {"path": "a.py"}, "x" * 20_000)

    out = format_tool_detail(item)

    assert "<b>read_file</b>" in out and "a.py" in out and len(out) < 4096
    assert "no result recorded" in format_tool_detail(HistoryItem("tool", "t", {}, None))


async def test_replies_are_sent_as_html_and_fall_back_to_plain_when_rejected() -> None:
    application = FakeApplication(events=_reply_events("run `ls` & **go**"))
    gateway, api = _gateway(application)
    await _say(gateway, "hi")
    assert "HTML" in api.modes
    assert any("<code>ls</code> &amp; <b>go</b>" in t for t in api.texts())

    application = FakeApplication(events=_reply_events("run `ls` & **go**"))
    gateway, api = _gateway(application)
    api.reject_html = True
    await _say(gateway, "hi")
    assert "run `ls` & **go**" in api.texts()  # the plain text, unconverted


async def test_a_huge_reply_is_split_and_never_exceeds_the_limit() -> None:
    application = FakeApplication(events=_reply_events("<&>\n" * 5000))
    gateway, api = _gateway(application)

    await _say(gateway, "hi")

    assert len(api.sent) > 2 and all(len(t) <= 4096 for t in api.texts())


async def test_the_command_menu_is_published_when_the_bot_starts() -> None:
    gateway, api = _gateway(FakeApplication())
    api.updates = [[]]

    async def stop_soon() -> None:
        await asyncio.sleep(0.01)
        gateway.stop()

    await asyncio.gather(gateway.run(), stop_soon())

    names = [name for name, _ in api.commands]
    assert {"new", "sessions", "tools", "verbose", "history", "rename", "delete"} <= set(names)


async def test_non_text_messages_get_a_polite_reply_and_strangers_none() -> None:
    gateway, api = _gateway(FakeApplication())
    photo = {
        "update_id": 9001,
        "message": {
            "message_id": 1,
            "from": {"id": OWNER},
            "chat": {"id": OWNER, "type": "private"},
            "photo": [],
        },
    }
    stranger = {
        "update_id": 9002,
        "message": {"message_id": 2, "from": {"id": 999}, "chat": {"id": 51}, "photo": []},
    }

    await gateway.handle_update(photo)
    await gateway.handle_update(stranger)

    assert api.texts() == ["I only understand text messages for now."]


def _many_sessions(n: int) -> list[SessionSummary]:
    return [SessionSummary(id=f"s{i}", title=f"Chat {i}") for i in range(n)]


async def test_sessions_are_paged_and_any_old_conversation_can_be_opened() -> None:
    application = FakeApplication()
    application.sessions = _many_sessions(14)
    application.histories["s13"] = [HistoryItem("user", "oldest"), HistoryItem("assistant", "ok")]
    gateway, api = _gateway(application)

    await _say(gateway, "/sessions")
    _, _, keyboard = api.sent[-1]
    rows = keyboard["inline_keyboard"]
    assert [r[0]["callback_data"] for r in rows[:-1]] == [f"sw:{i}" for i in range(6)]
    assert [b["callback_data"] for b in rows[-1]] == ["noop", "sp:1"]

    await gateway.handle_update(_callback("sp:2"))
    edited = api.edit_markups[-1]["inline_keyboard"]
    assert [r[0]["callback_data"] for r in edited[:-1]] == ["sw:12", "sw:13"]
    assert [b["callback_data"] for b in edited[-1]] == ["sp:1", "noop"]

    await gateway.handle_update(_callback("sw:13"))
    assert "Switched to “Chat 13”" in api.sent[-1][1]


async def test_opening_a_conversation_offers_its_tool_calls_and_the_viewer_shows_details() -> None:
    application = FakeApplication()
    application.sessions = [SessionSummary(id="tui-1", title="Fix bug")]
    application.histories["tui-1"] = [
        HistoryItem("user", "old question"),
        HistoryItem("tool", "list_dir", {"path": "."}, "old result"),
        HistoryItem("assistant", "old answer"),
        HistoryItem("user", "fix the bug"),
        HistoryItem("tool", "read_file", {"path": "a.py"}, "def f(): <1>"),
        HistoryItem("tool", "run_command", {"command": "pytest"}, "3 passed"),
        HistoryItem("assistant", "Fixed."),
    ]
    gateway, api = _gateway(application)
    await _say(gateway, "/sessions")
    await gateway.handle_update(_callback("sw:0"))

    buttons = [b["text"] for b in api.sent[-1][2]["inline_keyboard"][0]]
    assert "🔧 Tool calls (3)" in buttons and "🗑 Delete" in buttons

    await gateway.handle_update(_callback("tl:"))  # only the LAST turn's calls
    text, keyboard = api.sent[-1][1], api.sent[-1][2]
    assert text.startswith("2 tool call(s)")
    assert [r[0]["callback_data"] for r in keyboard["inline_keyboard"]] == ["td:0", "td:1"]

    await gateway.handle_update(_callback("td:0"))
    detail = api.sent[-1][1]
    assert "read_file" in detail and "a.py" in detail and "def f(): &lt;1&gt;" in detail
    assert api.sent[-1][2]["inline_keyboard"][0][0]["callback_data"] == "tl:"

    await gateway.handle_update(_callback("td:9"))
    assert "out of date" in api.sent[-1][1]


async def test_tools_command_reports_when_there_is_nothing_to_show() -> None:
    gateway, api = _gateway(FakeApplication())
    await _say(gateway, "/tools")
    assert "No conversation is open" in api.texts()[-1]

    application = FakeApplication(events=_reply_events())
    gateway, api = _gateway(application)
    await _say(gateway, "hello")
    await _say(gateway, "/tools")
    assert api.texts()[-1] == "The last turn made no tool calls."


async def test_the_finished_status_offers_the_runs_tool_calls() -> None:
    application = FakeApplication(events=_tool_events(2))
    gateway, api = _gateway(application)

    await _say(gateway, "read two")

    markup = api.edit_markups[-1]
    assert markup["inline_keyboard"][0][0] == {"text": "🔧 Tool calls (2)", "callback_data": "tl:"}

    quiet = FakeApplication(events=_reply_events())
    gateway, api = _gateway(quiet)
    await _say(gateway, "no tools")
    assert api.edit_markups[-1] is None


async def test_history_shows_the_latest_messages_of_the_open_conversation() -> None:
    application = FakeApplication(events=_reply_events())
    gateway, api = _gateway(application)
    await _say(gateway, "/history")
    assert "No conversation is open" in api.texts()[-1]

    await _say(gateway, "first")
    session = application.sent[0][0].session
    application.histories[session] = [
        HistoryItem("user", "q1"),
        HistoryItem("tool", "read_file", {}, "r"),
        HistoryItem("assistant", "a1 `code`"),
        HistoryItem("user", "q2"),
    ]
    await _say(gateway, "/history 2")

    out = api.texts()[-1]
    assert "a1 <code>code</code>" in out and "q2" in out and "q1" not in out


async def test_verbose_off_hides_tool_activity_and_full_shows_each_call() -> None:
    application = FakeApplication(events=_tool_events(2))
    gateway, api = _gateway(application)
    await _say(gateway, "/verbose off")
    await _say(gateway, "go")
    assert all("→" not in e[2] for e in api.edits)

    gateway, api = _gateway(FakeApplication(events=_tool_events(2)))
    await _say(gateway, "/verbose full")
    await _say(gateway, "go")
    tool_messages = [t for t in api.texts() if t.startswith("🔧")]
    assert len(tool_messages) == 2 and "f0.py" in tool_messages[0]
    assert any(e[2].startswith("🔧") and "✓ ok" in e[2] for e in api.edits)


async def test_verbose_rejects_unknown_values_and_offers_buttons() -> None:
    gateway, api = _gateway(FakeApplication())

    await _say(gateway, "/verbose loud")
    buttons = api.sent[-1][2]["inline_keyboard"][0]
    assert [b["callback_data"] for b in buttons] == ["vb:off", "vb:compact", "vb:full"]

    await gateway.handle_update(_callback("vb:full"))
    assert api.edits[-1][2] == "Tool activity: full."
    await gateway.handle_update(_callback("vb:bogus"))
    assert api.edits[-1][2] == "Tool activity: full."


async def test_rename_changes_the_title_of_the_open_conversation() -> None:
    application = FakeApplication(events=_reply_events())
    gateway, api = _gateway(application)
    await _say(gateway, "/rename x")
    assert "No conversation is open" in api.texts()[-1]

    await _say(gateway, "first")
    await _say(gateway, "/rename")
    assert api.texts()[-1].startswith("Usage")
    await _say(gateway, "/rename Better title")

    session = application.sent[0][0].session
    assert application.titles[session] == "Better title"
    assert api.texts()[-1] == "Renamed to “Better title”."


async def test_delete_asks_first_and_only_then_deletes() -> None:
    application = FakeApplication(events=_reply_events())
    gateway, api = _gateway(application)
    await _say(gateway, "first")
    session = application.sent[0][0].session

    await _say(gateway, "/delete")
    assert [b["callback_data"] for b in api.sent[-1][2]["inline_keyboard"][0]] == ["dl:y", "dl:n"]
    assert application.deleted == []

    await gateway.handle_update(_callback("dl:n"))
    assert application.deleted == [] and api.edits[-1][2] == "Kept."

    await gateway.handle_update(_callback("dl:y"))
    assert application.deleted == [session] and "Deleted" in api.edits[-1][2]
    await _say(gateway, "second")  # the next message starts a new conversation
    assert application.sent[1][0].session != session


async def test_delete_is_refused_during_a_run_and_without_a_conversation() -> None:
    gate = asyncio.Event()
    application = FakeApplication(events=[event(EventType.RUN_STARTED)], gate=gate)
    gateway, api = _gateway(application)
    await gateway.handle_update(_message("long"))
    await asyncio.sleep(0.05)

    await gateway.handle_update(_message("/delete"))
    await gateway.handle_update(_callback("dl:y"))

    assert application.deleted == []
    assert "in progress" in api.texts()[-1] and "Nothing to delete" in api.answers[-1][1]
    gate.set()
    await _settle(gateway)

    fresh, api2 = _gateway(FakeApplication())
    await _say(fresh, "/delete")
    assert api2.texts() == ["No conversation is open."]


async def test_status_says_where_you_are() -> None:
    gateway, api = _gateway(FakeApplication(events=_reply_events()))
    await _say(gateway, "/status")
    assert "a new draft" in api.texts()[-1] and "idle" in api.texts()[-1]
    await _say(gateway, "my topic")
    await _say(gateway, "/status")
    assert "“my topic”" in api.texts()[-1]


async def test_approval_shows_the_full_arguments_not_just_a_label() -> None:
    application = FakeApplication(
        events=[
            event(EventType.RUN_STARTED),
            event(
                EventType.APPROVAL_REQUIRED,
                approval_id="w1",
                tool="write_file",
                tool_call_id="c",
                args={"path": "x.py", "content": "print('<hi>')"},
            ),
        ],
        gate=asyncio.Event(),
    )
    gateway, api = _gateway(application)
    await gateway.handle_update(_message("write it"))
    await asyncio.sleep(0.05)

    text = api.sent[-1][1]
    assert "x.py" in text and "&lt;hi&gt;" in text
    application._gate.set()  # type: ignore[attr-defined]
    await _settle(gateway)


async def test_tool_arguments_are_redacted_before_they_reach_telegram(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_secret = "s3cr3t-env-value-123"
    bearer = "abcDEF1234567890xyz"
    monkeypatch.setenv("MY_SERVICE_TOKEN", env_secret)
    args = {
        "command": f"curl -H 'Authorization: Bearer {bearer}' https://x",
        "files": [{"content": f"KEY={env_secret}"}],
    }
    application = FakeApplication(
        events=[
            event(EventType.RUN_STARTED),
            event(EventType.TOOL_STARTED, tool="run_command", tool_call_id="c", args=args),
            event(
                EventType.APPROVAL_REQUIRED,
                approval_id="s1",
                tool="run_command",
                tool_call_id="c",
                args=args,
            ),
        ],
        gate=asyncio.Event(),
    )
    gateway, api = _gateway(application)
    await _say(gateway, "/verbose full")  # the full mode also sends each call's arguments
    await gateway.handle_update(_message("fetch it"))
    await asyncio.sleep(0.05)
    application._gate.set()  # type: ignore[attr-defined]
    await _settle(gateway)
    application.histories[application.sent[0][0].session or ""] = [
        HistoryItem("user", "fetch it"),
        HistoryItem("tool", "run_command", args, "ok"),
    ]
    await _say(gateway, "/tools")
    await gateway.handle_update(_callback("td:0"))

    shown = api.texts() + [text for _, _, text in api.edits]
    shown += [json.dumps(m) for _, _, m in api.sent if m]
    assert any("Approve?" in t and "[redacted]" in t for t in shown)
    assert not any(env_secret in t or bearer in t for t in shown)


async def test_the_open_conversation_survives_a_restart(tmp_path: Any) -> None:
    state = tmp_path / "state" / "telegram.json"
    application = FakeApplication(events=_reply_events())
    gateway, _ = _gateway(application, settings=_settings(state_path=state))
    await _say(gateway, "/verbose off")
    await _say(gateway, "first")
    session = application.sent[0][0].session

    reborn, api = _gateway(application, settings=_settings(state_path=state))
    await _say(reborn, "second")

    assert application.sent[1][0].session == session
    assert application.sessions[0].id == session and len(application.sessions) == 1
    assert all("→" not in e[2] for e in api.edits)  # tool mode restored too


async def test_a_conversation_deleted_while_the_bot_was_off_becomes_a_new_draft(
    tmp_path: Any,
) -> None:
    state = tmp_path / "telegram.json"
    state.write_text(
        json.dumps({str(OWNER): {"session": "gone", "title": "Old", "mode": "compact"}})
    )
    application = FakeApplication(events=_reply_events())
    gateway, _ = _gateway(application, settings=_settings(state_path=state))

    await _say(gateway, "hello again")

    assert application.sent[0][0].session != "gone"


async def test_a_corrupt_state_file_is_ignored(tmp_path: Any) -> None:
    state = tmp_path / "telegram.json"
    state.write_text("{not json")
    application = FakeApplication(events=_reply_events())
    gateway, _ = _gateway(application, settings=_settings(state_path=state))

    await _say(gateway, "hello")

    assert len(application.sent) == 1


def test_state_path_comes_from_the_environment() -> None:
    settings = TelegramSettings.from_env(
        {
            "TELEGRAM_BOT_TOKEN": TOKEN,
            "AGENT_STACK_TELEGRAM_USERS": "7",
            "AGENT_STACK_TELEGRAM_STATE": "/tmp/x/state.json",
        }
    )
    assert str(settings.state_path) == "/tmp/x/state.json"


# ── cost ─────────────────────────────────────────────────────────────────────
from agent_stack.gateways.base import SessionUsage  # noqa: E402

_SESSION = f"telegram:{OWNER}-new1"


def _usage(cost: float, calls: int = 2, priced: int | None = None) -> SessionUsage:
    return SessionUsage(
        calls=calls,
        priced_calls=calls if priced is None else priced,
        input_tokens=12_300,
        output_tokens=450,
        cost_usd=cost,
    )


async def test_cost_before_any_conversation_says_nothing_was_spent() -> None:
    gateway, api = _gateway(FakeApplication())

    await _say(gateway, "/cost")

    assert "nothing has been spent" in api.texts()[-1]


async def test_cost_command_shows_tokens_dollars_and_the_budget() -> None:
    application = FakeApplication(events=_reply_events())
    application.usages[_SESSION] = [_usage(0.0123)]
    gateway, api = _gateway(application)
    await _say(gateway, "hello")

    await _say(gateway, "/cost")

    text = api.texts()[-1]
    assert "$0.012 · 12.3k in · 450 out · 2 calls" in text
    assert "Budget" not in text


async def test_the_finished_status_shows_what_that_run_cost_including_subagents() -> None:
    application = FakeApplication(events=_reply_events())
    # before the run the conversation had cost $0.010; after it, $0.0142 (subagent calls
    # never reach this chat's event stream, so the durable totals are the truth)
    application.usages[_SESSION] = [_usage(0.010, 2), _usage(0.0142, 5)]
    gateway, api = _gateway(application)

    await _say(gateway, "go")

    assert api.edits[-1][2].startswith("✓ done · $0.0042")


async def test_an_unpriced_run_shows_no_dollar_figure() -> None:
    application = FakeApplication(events=_reply_events())
    application.usages[_SESSION] = [_usage(0.0, 0), _usage(0.0, 1, priced=0)]
    gateway, api = _gateway(application)

    await _say(gateway, "go")

    assert api.edits[-1][2].split("\n")[0] == "✓ done"


async def test_status_includes_the_spend_and_the_menu_lists_cost() -> None:
    application = FakeApplication(events=_reply_events())
    application.usages[_SESSION] = [_usage(0.5)]
    gateway, api = _gateway(application)
    await _say(gateway, "hi")
    await _say(gateway, "/status")

    assert "Spent: $0.500" in api.texts()[-1]
    await _say(gateway, "/help")
    assert "/cost" in api.texts()[-1]


async def test_a_failing_cost_lookup_never_breaks_a_run() -> None:
    application = FakeApplication(events=_reply_events("fine"))

    async def broken(session_id: str) -> SessionUsage:
        raise RuntimeError("db is down")

    application.session_usage = broken  # type: ignore[method-assign]
    gateway, api = _gateway(application)

    await _say(gateway, "go")

    assert "fine" in api.texts() and api.edits[-1][2] == "✓ done"


async def test_cost_shows_the_budget_when_one_is_set() -> None:
    application = FakeApplication(events=_reply_events())
    application.config.budget_usd = 2.0
    application.usages[_SESSION] = [_usage(0.5)]
    gateway, api = _gateway(application)
    await _say(gateway, "hello")

    await _say(gateway, "/cost")

    assert "Budget: $0.500 of $2.00" in api.texts()[-1]


# ── only the owner, only in their own private chat ───────────────────────────
import logging  # noqa: E402


def _group_message(text: str, *, kind: str = "supergroup") -> dict[str, Any]:
    update_id = next(_UPDATE_IDS)
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "from": {"id": OWNER},  # the owner, but speaking in a group
            "chat": {"id": -100123, "type": kind},
            "text": text,
        },
    }


async def test_the_owner_is_ignored_in_groups_and_channels_so_nothing_leaks_to_members() -> None:
    application = FakeApplication(events=_reply_events("secret output"))
    gateway, api = _gateway(application)

    for kind in ("group", "supergroup", "channel"):
        await gateway.handle_update(_group_message("list my files", kind=kind))
        await gateway.handle_update(_group_message("/sessions", kind=kind))

    assert api.sent == [] and api.edits == [] and application.sent == []


async def test_a_message_that_is_not_in_the_senders_own_private_chat_is_ignored() -> None:
    application = FakeApplication(events=_reply_events())
    gateway, api = _gateway(application)
    spoofed = _message("hello", chat=12345)  # a private-typed chat that is not the sender's own
    no_type = _message("hello")
    del no_type["message"]["chat"]["type"]
    no_sender = _message("hello")
    del no_sender["message"]["from"]

    for update in (spoofed, no_type, no_sender):
        await gateway.handle_update(update)
    await asyncio.sleep(0.1)  # a wrongly accepted message would have started its run by now

    assert api.sent == [] and application.sent == []


async def test_a_button_tap_from_a_group_message_is_ignored_even_from_the_owner() -> None:
    gateway, api, application, gate = await _start_pending_approval()
    tap = _callback("ap:a1b2c3:y")
    tap["callback_query"]["message"]["chat"] = {"id": -100123, "type": "supergroup"}

    await gateway.handle_update(tap)

    assert application.decisions == [] and api.answers == []
    gate.set()
    await _settle(gateway)


async def test_a_stranger_gets_no_reply_and_the_owner_sees_it_in_the_local_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    application = FakeApplication(events=_reply_events())
    now = [1000.0]
    api = FakeApi()
    gateway = TelegramGateway(
        application,  # type: ignore[arg-type]
        api,  # type: ignore[arg-type]
        _settings(),
        edit_interval=0.0,
        clock=lambda: now[0],
    )

    with caplog.at_level(logging.WARNING, logger="agent_stack.telegram"):
        for _ in range(5):  # a flood from one stranger is logged once a minute
            await gateway.handle_update(
                _message("delete everything, my password is hunter2", user=999)
            )
        now[0] += 61
        await gateway.handle_update(_message("again", user=999))
        await gateway.handle_update(_message("hi", user=1000))  # a different stranger

    assert api.sent == [] and application.sent == []
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 3
    assert all("allow-list" in line for line in lines)
    assert any("999" in line for line in lines) and any("1000" in line for line in lines)
    assert not any("hunter2" in line or "delete everything" in line for line in lines)  # no content


async def test_the_owner_in_their_private_chat_still_works_and_is_not_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    application = FakeApplication(events=_reply_events("fine"))
    gateway, api = _gateway(application)

    with caplog.at_level(logging.WARNING, logger="agent_stack.telegram"):
        await _say(gateway, "hello")

    assert "fine" in api.texts() and not caplog.records
