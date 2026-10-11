"""The line REPL gateway.

A thin, scriptable gateway with the same event consumption as the TUI: read a line,
``Application.send_message``, print the returned ``AgentEvent`` stream as Rich text,
and leave on ``Ctrl+C``, ``exit``/``quit``, or EOF. It touches only ``Application``,
``gateways.base`` and ``core.events``.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from typing import TextIO

from rich.console import Console

from agent_stack.app.application import Application, IdentityResolver, SessionResolver
from agent_stack.core.events import AgentEvent, EventType
from agent_stack.gateways.base import MessageEnvelope
from agent_stack.gateways.tui.render import (
    assistant_block,
    reasoning_block,
    run_status_block,
    subagent_block,
    tool_completed_block,
    tool_output_text,
    tool_started_block,
)

__all__ = ["LineGateway", "print_event", "run_cli"]

_QUIT_WORDS = frozenset({"exit", "quit"})


def print_event(console: Console, event: AgentEvent) -> None:
    """Render one event to the console (the REPL's whole event vocabulary)."""
    if event.type is EventType.ASSISTANT_MESSAGE:
        console.print(assistant_block(event))
    elif event.type is EventType.MODEL_REASONING:
        console.print(reasoning_block(event))
    elif event.type is EventType.TOOL_STARTED:
        console.print(tool_started_block(event))
    elif event.type is EventType.TOOL_OUTPUT:
        console.print(tool_output_text(event), end="")
    elif event.type is EventType.TOOL_COMPLETED:
        console.print(tool_completed_block(event))
    elif event.type is EventType.RUN_FAILED:
        console.print(run_status_block(event))
    elif event.type is EventType.SUBAGENT_STARTED:
        console.print(subagent_block(event, done=False))
    elif event.type is EventType.SUBAGENT_COMPLETED:
        console.print(subagent_block(event, done=True))


class LineGateway:
    """The REPL gateway: drive one message and stream its events to the console."""

    def __init__(self, out: Console | None = None) -> None:
        self._out = out if out is not None else Console()

    async def handle_message(
        self,
        application: Application,
        envelope: MessageEnvelope,
    ) -> AsyncIterator[AgentEvent]:
        """Dispatch one message and print each event as it arrives."""
        handle = await application.send_message(envelope)
        async for event in handle.events():
            print_event(self._out, event)
            yield event


async def run_cli(
    application: Application,
    *,
    principal: str = "local-user",
    instream: TextIO | None = None,
    out: Console | None = None,
) -> None:
    """Read lines from ``instream`` and run each through the application until EOF."""
    stream = instream if instream is not None else sys.stdin
    console = out if out is not None else Console()
    gateway = LineGateway(out=console)

    identity = IdentityResolver()
    principal_id = identity.resolve("cli", principal)
    session = await SessionResolver(application.session_store, application.agent.name).resolve(
        principal_id
    )

    console.print(
        f"agent-stack chat (agent={application.agent.name}) — type 'exit' or Ctrl-D to leave."
    )
    while True:
        console.print("[bold cyan]>[/bold cyan] ", end="")
        try:
            line = stream.readline()
        except KeyboardInterrupt:
            break
        if not line:
            console.print("")
            break
        text = line.strip()
        if not text:
            continue
        if text in _QUIT_WORDS:
            break

        envelope = MessageEnvelope(principal=principal_id, session=session, text=text)
        try:
            async for _event in gateway.handle_message(application, envelope):
                pass
        except KeyboardInterrupt:
            break
    console.print("[dim]bye[/dim]")
