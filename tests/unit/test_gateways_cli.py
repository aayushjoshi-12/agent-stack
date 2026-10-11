"""Tests for the line REPL gateway: canned events printed as Rich text, clean EOF exit."""

from __future__ import annotations

import io

from rich.console import Console

from agent_stack.core.events import EventType
from agent_stack.gateways.cli import run_cli
from tests.unit.gateway_fakes import FakeApplication, event


async def test_cli_repl_streams_events() -> None:
    application = FakeApplication(
        events=[
            event(EventType.RUN_STARTED),
            event(EventType.ASSISTANT_MESSAGE, content="hello from the repl"),
            event(EventType.RUN_COMPLETED, steps=1, tool_calls=0),
        ]
    )
    instream = io.StringIO("do a thing\nexit\n")
    buffer = io.StringIO()
    console = Console(file=buffer, no_color=True, width=120, highlight=False)

    await run_cli(application, instream=instream, out=console)  # type: ignore[arg-type]

    output = buffer.getvalue()
    assert "hello from the repl" in output
    # The message drove one run through the Application.
    assert len(application.sent) == 1
    assert application.sent[0][0].text == "do a thing"
    # EOF/'exit' left cleanly.
    assert "bye" in output
