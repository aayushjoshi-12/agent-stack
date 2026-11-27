"""P27: redaction, structured logs, metrics, and the "no secret reaches a log" guarantee."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agent_stack.core.agent import Agent
from agent_stack.core.event_bus import InMemoryEventBus
from agent_stack.core.events import AgentEvent, EventType
from agent_stack.core.run_context import RunContext, run_context
from agent_stack.observability.audit import AuditLogger, audit_record, read_audit
from agent_stack.observability.logging import EventLogger, JsonLogFormatter, configure_logging
from agent_stack.observability.metrics import Metrics
from agent_stack.observability.redact import REDACTED, StreamRedactor, env_secrets, redact
from agent_stack.observability.sinks import FanoutSink

SECRET_KEY = "sk-or-v1-0123456789abcdef0123456789abcdef"


def _event(
    kind: EventType, run_id: str = "r1", parent: str | None = None, **data: Any
) -> AgentEvent:
    return AgentEvent(
        id="e",
        type=kind,
        run_id=run_id,
        session_id="s1",
        sequence=0,
        timestamp=datetime.now(UTC),
        data=data,
        parent_run_id=parent,
    )


# ── redaction ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "text",
    [
        f"key is {SECRET_KEY} ok",
        "token ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "aws AKIAABCDEFGHIJKLMNOP here",
        "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6",
        "Authorization: Bearer abcdefghijklmnop1234567890",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAK\n-----END RSA PRIVATE KEY-----",
    ],
)
def test_redact_removes_common_secret_shapes(text: str) -> None:
    redacted = redact(text)

    assert REDACTED in redacted
    for fragment in ("sk-or-v1", "ghp_", "AKIA", "eyJ", "abcdefghijklmnop", "MIIBOgIBAAJBAK"):
        assert fragment not in redacted


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("api_key=hunter2hunter2", "api_key="),
        ('{"password": "correct-horse"}', '"password": "'),
        ("TOKEN: abcd1234", "TOKEN: "),
        ("export OPENAI_API_KEY=abc123xyz", "OPENAI_API_KEY="),
    ],
)
def test_redact_keeps_the_name_and_drops_the_value(text: str, kept: str) -> None:
    redacted = redact(text)

    assert kept in redacted
    assert redacted.count(REDACTED) == 1
    for value in ("hunter2hunter2", "correct-horse", "abcd1234", "abc123xyz"):
        assert value not in redacted


def test_redact_leaves_ordinary_text_alone() -> None:
    text = "Listing 3 files in src/agent_stack; the token budget is fine and keys are sorted."

    assert redact(text) == text


def test_redact_replaces_known_secret_values_in_any_shape() -> None:
    assert redact("db pw is zebra-quartz-9921!", known=["zebra-quartz-9921!"]) == (
        f"db pw is {REDACTED}"
    )


def _stream(redactor: StreamRedactor, pieces: list[str]) -> list[str]:
    return [redactor.push(piece) for piece in pieces]


def test_stream_redactor_catches_a_key_split_across_pieces() -> None:
    prose = "Here is the configuration you asked for, written out in full below. "
    pieces = [
        prose,
        "OPENROUTER=sk-or-v1-0123",
        "456789abcdef0123456789abcdef",
        " done. " + "x" * 80,
    ]

    shown = "".join(_stream(StreamRedactor(), pieces))

    assert "sk-or" not in shown and "0123" not in shown
    assert shown.startswith(prose)
    assert REDACTED in shown


def test_stream_redactor_holds_back_the_start_of_a_known_secret() -> None:
    secret = "zebra-quartz-9921!" * 6  # longer than the default hold
    redactor = StreamRedactor(known=[secret])
    pieces = ["a" * 100, secret[:50], secret[50:], " and the rest " + "b" * 80]

    shown = _stream(redactor, pieces)

    assert "zebra" not in shown[1]  # the half secret is held, not shown
    assert "zebra" not in "".join(shown)
    assert REDACTED in "".join(shown)


def test_stream_redactor_shows_plain_text_with_only_a_short_tail_held() -> None:
    text = "word " * 40
    redactor = StreamRedactor(hold=64)

    shown = "".join(redactor.push(text[i : i + 7]) for i in range(0, len(text), 7))

    assert text.startswith(shown) and len(text) - len(shown) == 64


def test_stream_redactor_stops_when_a_redaction_reaches_back_into_shown_text() -> None:
    redactor = StreamRedactor(hold=4)  # too short for a Bearer token: shows the reach-back
    assert redactor.push("Bearer abcdefgh") == "Bearer abcd"

    assert redactor.push("ijklmnop") == ""  # now a token: the shown text no longer matches
    assert redactor.push(" more text after it, long enough to pass the hold") == ""


def test_env_secrets_picks_secret_looking_variables_only() -> None:
    env = {
        "OPENROUTER_API_KEY": "or-key-value-123",
        "DB_PASSWORD": "pw-value-12345",
        "SHORT_TOKEN": "tiny",  # too short to redact without eating ordinary words
        "HOME": "/Users/someone/home",
        "EDITOR": "neovim-editor",
    }

    assert set(env_secrets(env)) == {"or-key-value-123", "pw-value-12345"}


# ── the audit log (P8b) now redacts values, not just key names ───────────────
def test_audit_record_redacts_secret_values_inside_free_text() -> None:
    event = _event(
        EventType.TOOL_STARTED,
        tool="run_command",
        args={"command": f"curl -H 'Authorization: Bearer {SECRET_KEY}' https://x"},
    )

    line = json.dumps(audit_record(event, known=[]))

    assert SECRET_KEY not in line and REDACTED in line


def test_audit_record_redacts_known_env_secret_in_tool_output() -> None:
    event = _event(EventType.TOOL_OUTPUT, chunk="DB=postgres://u:zebra-quartz-9921!@h/db")

    line = json.dumps(audit_record(event, known=["zebra-quartz-9921!"]))

    assert "zebra-quartz-9921" not in line


# ── structured JSON logs ─────────────────────────────────────────────────────
def _record(message: str, **extra: Any) -> logging.LogRecord:
    record = logging.LogRecord("agent_stack.test", logging.INFO, __file__, 1, message, None, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_formatter_emits_one_redacted_line_with_run_identity() -> None:
    formatter = JsonLogFormatter(known_secrets=[])
    line = formatter.format(
        _record(f"calling with {SECRET_KEY}", run_id="r1", session_id="s1", trace_id="r0")
    )

    entry = json.loads(line)
    assert "\n" not in line
    assert (entry["level"], entry["logger"]) == ("info", "agent_stack.test")
    assert (entry["run_id"], entry["session_id"], entry["trace_id"]) == ("r1", "s1", "r0")
    assert SECRET_KEY not in line and REDACTED in entry["message"]


def test_json_formatter_takes_run_identity_from_the_current_run() -> None:
    context = RunContext(
        run_id="ctx-run",
        session_id="ctx-session",
        user_id="u",
        depth=0,
        parent_run_id=None,
        agent=Agent(name="a", model="m"),
        services=None,  # type: ignore[arg-type]
    )
    with run_context(context):
        entry = json.loads(JsonLogFormatter(known_secrets=[]).format(_record("hello")))

    assert (entry["run_id"], entry["session_id"]) == ("ctx-run", "ctx-session")
    assert "run_id" not in json.loads(JsonLogFormatter(known_secrets=[]).format(_record("hi")))


def test_json_formatter_redacts_exceptions_and_data() -> None:
    formatter = JsonLogFormatter(known_secrets=["zebra-quartz-9921!"])
    try:
        raise RuntimeError("boom zebra-quartz-9921!")
    except RuntimeError:
        import sys

        record = _record("failed", data={"cmd": f"echo {SECRET_KEY}"})
        record.exc_info = sys.exc_info()
    line = formatter.format(record)

    assert "zebra-quartz-9921" not in line and SECRET_KEY not in line


@pytest.fixture
def agent_logger() -> Iterator[logging.Logger]:
    logger = logging.getLogger("agent_stack")
    before = list(logger.handlers)
    yield logger
    for handler in list(logger.handlers):
        if handler not in before:
            handler.close()
            logger.removeHandler(handler)


def test_configure_logging_writes_json_lines_once_per_file(
    tmp_path: Path, agent_logger: logging.Logger
) -> None:
    path = tmp_path / "logs" / "agent.jsonl"

    configure_logging(path)
    configure_logging(path)  # idempotent: a second build_application must not double-log
    logging.getLogger("agent_stack.x").info("hello")

    lines = path.read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["message"] == "hello"
    assert agent_logger.propagate is False  # never into a TUI's terminal


def test_event_logger_groups_a_subagent_under_the_root_run(
    tmp_path: Path, agent_logger: logging.Logger
) -> None:
    path = tmp_path / "agent.jsonl"
    configure_logging(path)
    sink = EventLogger()

    sink.write(_event(EventType.RUN_STARTED, "root"))
    sink.write(_event(EventType.RUN_STARTED, "child", parent="root"))
    sink.write(_event(EventType.RUN_STARTED, "grandchild", parent="child"))
    sink.write(_event(EventType.RUN_FAILED, "root", error="boom"))

    entries = [json.loads(line) for line in path.read_text().splitlines()]
    assert {e["run_id"]: e["trace_id"] for e in entries} == {
        "root": "root",
        "child": "root",
        "grandchild": "root",
    }
    assert [e["level"] for e in entries] == ["info", "info", "info", "warning"]


# ── metrics ──────────────────────────────────────────────────────────────────
def test_metrics_count_runs_tools_and_approvals_from_events() -> None:
    metrics = Metrics()
    for kind, data in (
        (EventType.RUN_STARTED, {}),
        (EventType.TOOL_STARTED, {"tool": "run_command", "tool_call_id": "c1"}),
        (EventType.TOOL_COMPLETED, {"tool": "run_command", "tool_call_id": "c1", "ok": False}),
        (EventType.TOOL_STARTED, {"tool": "read_file", "tool_call_id": "c2"}),
        (EventType.TOOL_COMPLETED, {"tool": "read_file", "tool_call_id": "c2", "ok": True}),
        (EventType.APPROVAL_REQUIRED, {"approval_id": "a"}),
        (EventType.APPROVAL_RESOLVED, {"approval_id": "a", "status": "rejected"}),
        (EventType.RUN_COMPLETED, {}),
    ):
        metrics.write(_event(kind, **data))

    snapshot = metrics.snapshot()
    assert snapshot["counters"] == {
        "runs_started": 1,
        "runs_completed": 1,
        "tool_calls.run_command": 1,
        "tool_calls.read_file": 1,
        "tool_failures.run_command": 1,
        "approvals_required": 1,
        "approvals_rejected": 1,
    }
    assert snapshot["timings"]["run_seconds"]["count"] == 1
    assert snapshot["timings"]["tool_seconds"]["count"] == 2


def test_metrics_count_failed_runs() -> None:
    metrics = Metrics()
    metrics.write(_event(EventType.RUN_STARTED))
    metrics.write(_event(EventType.RUN_FAILED, error="x"))

    assert metrics.snapshot()["counters"] == {"runs_started": 1, "runs_failed": 1}


def test_fanout_sink_survives_a_failing_sink() -> None:
    seen: list[AgentEvent] = []

    class Boom:
        def write(self, event: AgentEvent) -> None:
            raise RuntimeError("sink down")

    class Collect:
        def write(self, event: AgentEvent) -> None:
            seen.append(event)

    FanoutSink(Boom(), Collect()).write(_event(EventType.RUN_STARTED))

    assert len(seen) == 1


# ── acceptance: no secret appears in any log ─────────────────────────────────
async def test_no_secret_appears_in_the_audit_or_structured_log(
    tmp_path: Path, agent_logger: logging.Logger, monkeypatch: pytest.MonkeyPatch
) -> None:
    password = "zebra-quartz-9921!"
    monkeypatch.setenv("DEMO_DB_PASSWORD", password)
    log_path = tmp_path / "agent.jsonl"
    configure_logging(log_path)
    audit = AuditLogger(tmp_path / "audit.jsonl")
    bus = InMemoryEventBus(audit=FanoutSink(audit, EventLogger(), Metrics()))

    await bus.publish(
        _event(
            EventType.TOOL_STARTED,
            tool="run_command",
            tool_call_id="c1",
            args={"command": f"curl -H 'Authorization: Bearer {SECRET_KEY}' https://x"},
        )
    )
    await bus.publish(
        _event(EventType.TOOL_OUTPUT, tool_call_id="c1", chunk=f"connected as admin/{password}\n")
    )
    await bus.publish(_event(EventType.RUN_FAILED, error=f"RuntimeError('{SECRET_KEY}')"))
    audit.close()

    everything = log_path.read_text() + (tmp_path / "audit.jsonl").read_text()
    assert SECRET_KEY not in everything
    assert password not in everything
    assert read_audit(tmp_path / "audit.jsonl")  # and the logs are not simply empty
