"""The line REPL gateway.

A thin, scriptable gateway with the same event consumption as the TUI: read a line,
``Application.send_message``, print the returned ``AgentEvent`` stream as Rich text,
and leave on ``Ctrl+C``, ``exit``/``quit``, or EOF. It touches only ``Application``,
``gateways.base`` and ``core.events``.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TextIO

from rich.console import Console
from rich.markup import escape
from rich.text import Text

from agent_stack.app.application import (
    Application,
    IdentityResolver,
    SessionResolver,
    parse_export_args,
)
from agent_stack.core.events import AgentEvent, EventType
from agent_stack.gateways.base import MessageEnvelope, image_paths, load_images, usage_summary
from agent_stack.gateways.tui.render import (
    assistant_block,
    failure_block,
    reasoning_block,
    subagent_block,
    tool_args_lines,
    tool_detail,
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
        console.print(Text(tool_output_text(event)), end="")  # raw text: never markup
    elif event.type is EventType.RUN_FAILED:
        console.print(failure_block(event))
    elif event.type is EventType.APPROVAL_REQUIRED:
        tool = str(event.data.get("tool", "tool"))
        args = event.data.get("args")
        detail = tool_detail(args) if tool == "run_command" else tool_args_lines(args)
        console.print(f"[bold yellow]approval needed:[/] {escape(tool)} {escape(detail)}")
    elif event.type is EventType.SUBAGENT_STARTED:
        console.print(subagent_block(event, done=False))
    elif event.type is EventType.SUBAGENT_COMPLETED:
        console.print(subagent_block(event, done=True))
    elif event.type is EventType.CONTEXT_COMPACTED:
        console.print(
            f"[dim]context compacted: {event.data.get('messages', 0)} messages summarised[/]"
        )
    elif event.type is EventType.MODEL_RETRY:
        console.print(f"[dim]{escape(str(event.data.get('text', 'retrying the model call')))}[/]")


#: Answers an ``approval.required`` event: ``True`` approves, ``False`` rejects.
Approver = Callable[[AgentEvent], Awaitable[bool]]


class LineGateway:
    """The REPL gateway: drive one message and stream its events to the console.

    ``approver`` decides gated tool calls. Without one every request is **rejected**, so an
    unattended run can neither hang on a prompt nobody sees nor approve itself.
    """

    def __init__(self, out: Console | None = None, approver: Approver | None = None) -> None:
        self._out = out if out is not None else Console()
        self._approver = approver

    async def handle_message(
        self,
        application: Application,
        envelope: MessageEnvelope,
    ) -> AsyncIterator[AgentEvent]:
        """Dispatch one message and print each event as it arrives."""
        handle = await application.send_message(envelope)
        async for event in handle.events():
            print_event(self._out, event)
            if event.type is EventType.APPROVAL_REQUIRED:
                approved = await self._approver(event) if self._approver is not None else False
                try:
                    application.decide_approval(
                        str(event.data["approval_id"]),
                        run_id=event.run_id,
                        session_id=event.session_id,
                        principal=envelope.principal,
                        approve=approved,
                    )
                except Exception as exc:  # e.g. the run was cancelled while we asked
                    self._out.print(f"[yellow]approval not applied:[/] {escape(str(exc))}")
            yield event
        await self.print_cost(application, envelope.session)

    async def print_cost(self, application: Application, session_id: str) -> None:
        """One dim line after each answer: what this conversation has cost so far."""
        try:
            usage = await application.session_usage(session_id)
        except Exception:  # a cost line must never turn a finished answer into an error
            return
        if usage.calls:
            self._out.print(f"[dim]spent so far: {usage_summary(usage)}[/dim]")


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

    async def ask(event: AgentEvent) -> bool:
        """Prompt on the console; only an explicit ``y`` approves."""
        console.print("[bold]approve? [y/N][/bold] ", end="")
        answer = await asyncio.to_thread(stream.readline)
        return answer.strip().lower() in {"y", "yes"}

    gateway = LineGateway(out=console, approver=ask)

    identity = IdentityResolver()
    principal_id = identity.resolve("cli", principal)
    session = await SessionResolver(application.session_store, application.agent.name).resolve(
        principal_id
    )

    console.print(
        f"agent-stack chat (agent={application.agent.name}) — type 'exit' or Ctrl-D to leave."
    )
    try:
        while True:
            console.print("[bold]>[/bold] ", end="")
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
            if text == "/cost":
                await gateway.print_cost(application, session)
                continue
            if text == "/compact" or text.startswith("/compact "):
                try:
                    count = await application.compact_session(session, text[len("/compact") :])
                    console.print(
                        f"[dim]compacted {count} messages[/]"
                        if count
                        else "[dim]nothing to compact[/]"
                    )
                except Exception as exc:
                    console.print(f"[red]cannot compact:[/] {escape(str(exc))}")
                continue

            if text == "/export" or text.startswith("/export "):
                fmt, path = parse_export_args(text[len("/export") :])
                try:
                    saved = await application.save_export(session, fmt, path)
                    console.print(f"[dim]exported to {escape(str(saved))}[/]")
                except Exception as exc:
                    console.print(f"[red]cannot export:[/] {escape(str(exc))}")
                continue

            text, paths = image_paths(text)  # "@shot.png" or a pasted path attaches it (P44)
            try:
                images = load_images(paths)
            except (OSError, ValueError) as exc:
                console.print(f"[red]cannot attach:[/] {escape(str(exc))}")
                continue
            for image in images:
                console.print(f"[dim]\\[image: {escape(image.name)}][/]")
            envelope = MessageEnvelope(
                principal=principal_id, session=session, text=text, images=images
            )
            try:
                async for _event in gateway.handle_message(application, envelope):
                    pass
            except KeyboardInterrupt:
                break
    finally:
        await application.drain_background()  # MCP servers
    console.print("[dim]bye[/dim]")


#: Prefixed to the task in one-shot mode. Seen in a benchmark: with nobody to answer, the agent
#: ended a task by listing steps "left for you", and stopped before checking its own work.
UNATTENDED_NOTICE = (
    "You are running unattended: nobody can answer questions or do any step for you. Do the whole "
    "task yourself, including installs and configuration. Before you finish, check the result by "
    "running it (the tests, a real request, or the exact command the task names) and fix what "
    "fails. Never end with steps left for the user.\n\nTask:\n"
)


async def run_once(
    application: Application,
    prompt: str,
    *,
    approve_all: bool = False,
    principal: str = "cli-run",
    out: Console | None = None,
) -> int:
    """One-shot, non-interactive run: stream the events, return 0 on success, 1 on failure.

    Gated tools are rejected unless ``approve_all`` (the caller vouches for the sandbox).
    """
    console = out if out is not None else Console()

    async def decide(_event: AgentEvent) -> bool:
        return approve_all

    session = await application.new_session(principal)
    envelope = MessageEnvelope(
        principal=principal, session=session, text=UNATTENDED_NOTICE + prompt
    )
    gateway = LineGateway(out=console, approver=decide)
    try:
        async for _event in gateway.handle_message(application, envelope):
            pass
    except Exception as exc:
        console.print(f"[red]run failed:[/] {type(exc).__name__}: {escape(str(exc))}")
        return 1
    finally:
        await application.drain_background()  # MCP servers
    return 0
