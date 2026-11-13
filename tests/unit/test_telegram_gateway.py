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

    async def send_message(
        self, chat_id: int, text: str, reply_markup: dict[str, Any] | None = None
    ) -> int:
        self._next_id += 1
        self.sent.append((chat_id, text, reply_markup))
        return self._next_id

    async def edit_message_text(
        self, chat_id: int, message_id: int, text: str, reply_markup: Any = None
    ) -> None:
        self.edits.append((chat_id, message_id, text))

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
    text: str, *, user: int = OWNER, chat: int = 50, message_id: int | None = None
) -> dict[str, Any]:
    update_id = next(_UPDATE_IDS)
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id or update_id,
            "from": {"id": user},
            "chat": {"id": chat},
            "text": text,
        },
    }


def _callback(data: str, *, user: int = OWNER, chat: int = 50) -> dict[str, Any]:
    return {
        "update_id": next(_UPDATE_IDS),
        "callback_query": {
            "id": f"cb-{data}",
            "from": {"id": user},
            "data": data,
            "message": {"chat": {"id": chat}, "message_id": 1},
        },
    }


async def _settle(gateway: TelegramGateway, chat: int = 50) -> None:
    """Wait for the chat's run task (if any) to finish."""
    state = gateway._chats.get(chat)
    if state is not None and state.task is not None:
        await asyncio.wait_for(state.task, 2.0)


async def _say(gateway: TelegramGateway, text: str, **kwargs: Any) -> None:
    await gateway.handle_update(_message(text, **kwargs))
    await _settle(gateway, kwargs.get("chat", 50))


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
    assert envelope.metadata == {"idempotency_key": "message:50:42"}
    assert application.titles[envelope.session] == "fix the retry bug"
    assert "here you go" in api.texts()
    assert api.actions == [50]  # a typing indicator
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
    assert gateway._chats[50].busy is False
    assert gateway._chats[50].run_id is None


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
