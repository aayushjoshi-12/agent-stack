from __future__ import annotations

from datetime import datetime, timedelta

from agent_stack.gateways.base import format_when
from agent_stack.observability import cli


def test_format_when_is_local_and_relative() -> None:
    now = datetime(2026, 11, 25, 15, 0).astimezone()

    def at(**delta: float) -> str:
        return (now - timedelta(**delta)).isoformat()

    assert format_when(at(hours=1), now=now) == "today 14:00"
    assert format_when(at(days=1), now=now) == "yesterday 15:00"
    assert format_when(at(days=11), now=now) == "14 Nov 15:00"
    assert format_when(at(days=400), now=now).startswith("21 Oct 2025")
    assert format_when(None) == "" and format_when("not a date") == ""


def test_the_cli_knows_resume_and_tui_session() -> None:
    parser = cli._parser()
    assert parser.parse_args(["resume"]).session is None
    assert parser.parse_args(["resume", "tui:local-user-ab12"]).session == "tui:local-user-ab12"
    assert parser.parse_args(["tui", "--session", "x"]).session == "x"
