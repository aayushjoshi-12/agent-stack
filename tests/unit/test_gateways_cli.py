"""Tests for the line REPL gateway: canned events printed as Rich text, clean EOF exit."""

from __future__ import annotations

import io
from typing import Any

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


def _approval_events() -> list[Any]:
    return [
        event(EventType.RUN_STARTED),
        event(
            EventType.APPROVAL_REQUIRED,
            approval_id="a1",
            tool="run_command",
            tool_call_id="c1",
            args={"command": "rm -rf build"},
        ),
        event(EventType.RUN_COMPLETED, steps=1, tool_calls=1),
    ]


async def _run_with_input(application: FakeApplication, text: str) -> str:
    buffer = io.StringIO()
    console = Console(file=buffer, no_color=True, width=120, highlight=False)
    await run_cli(application, instream=io.StringIO(text), out=console)  # type: ignore[arg-type]
    return buffer.getvalue()


async def test_cli_asks_and_only_an_explicit_y_approves() -> None:
    application = FakeApplication(events=_approval_events())

    output = await _run_with_input(application, "go\ny\nexit\n")

    assert "approval needed: run_command rm -rf build" in output
    ((approval_id, run_id, _session, principal, approved),) = application.decisions
    assert (approval_id, approved) == ("a1", True)
    assert run_id == application.sent[0][1]
    assert principal == application.sent[0][0].principal


async def test_cli_anything_but_y_rejects() -> None:
    application = FakeApplication(events=_approval_events())

    await _run_with_input(application, "go\nmaybe\nexit\n")

    assert [d[-1] for d in application.decisions] == [False]


async def test_cli_gateway_without_an_approver_rejects_instead_of_hanging() -> None:
    from agent_stack.gateways.base import MessageEnvelope
    from agent_stack.gateways.cli import LineGateway

    application = FakeApplication(events=_approval_events())
    gateway = LineGateway(out=Console(file=io.StringIO()))

    async for _ in gateway.handle_message(
        application,  # type: ignore[arg-type]
        MessageEnvelope(principal="p", session="s", text="go"),
    ):
        pass

    assert [d[-1] for d in application.decisions] == [False]


async def test_cli_survives_a_refused_approval_decision() -> None:
    """Regression: an ApprovalError (e.g. run cancelled while asking) aborted the REPL turn."""
    application = FakeApplication(events=_approval_events())

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("approval already cancelled")

    application.decide_approval = refuse  # type: ignore[method-assign]

    output = await _run_with_input(application, "go\ny\nexit\n")

    assert "approval not applied" in output and "bye" in output
