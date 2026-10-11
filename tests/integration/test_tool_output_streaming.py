"""Live ``tool.output`` streaming (P14) — chunks must arrive while a command runs.

These drive real subprocesses, so they live under ``integration``.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from agent_stack.core.agent import Agent
from agent_stack.core.event_bus import InMemoryEventBus
from agent_stack.core.events import AgentEvent, EventType
from agent_stack.core.limits import AgentLimits
from agent_stack.core.runtime import AgentRuntime
from agent_stack.core.session import InMemorySessionStore
from agent_stack.core.workspace import WorkspacePolicy
from agent_stack.tools.builtins.terminal import run_command, run_command_tool
from agent_stack.tools.executor import ToolExecutor
from agent_stack.tools.registry import ToolRegistry
from tests.fakes import FakeChatModel, ai, call

pytestmark = pytest.mark.integration


class RecordingEventBus(InMemoryEventBus):
    """An ``InMemoryEventBus`` that also remembers every published event."""

    def __init__(self) -> None:
        super().__init__()
        self.published: list[AgentEvent] = []

    async def publish(self, event: AgentEvent) -> None:
        self.published.append(event)
        await super().publish(event)


def _build_runtime(
    model: Any, tool: Any, store: InMemorySessionStore, bus: InMemoryEventBus
) -> AgentRuntime:
    tools = (tool,)
    agent = Agent(
        name="assistant",
        model="test:model",
        tools=tools,  # type: ignore[arg-type]
        system_prompt="You are a test assistant.",
        limits=AgentLimits(),
    )
    return AgentRuntime(
        agent=agent,
        model=model,
        tool_executor=ToolExecutor(ToolRegistry(tools)),  # type: ignore[arg-type]
        session_store=store,
        event_bus=bus,
    )


async def test_command_output_streams_as_events(tmp_workspace: Path) -> None:
    """Output chunks are published as ``tool.output`` events *before* ``tool.completed``."""
    bus = RecordingEventBus()
    tool = run_command_tool(WorkspacePolicy(tmp_workspace))
    model = FakeChatModel(
        [
            ai(tool_calls=[call("run_command", {"command": "echo hello-stream", "shell": True})]),
            ai("done"),
        ]
    )
    runtime = _build_runtime(model, tool, InMemorySessionStore(), bus)

    await runtime.run(session_id="s1", user_id="u1", message="go")

    types = [event.type for event in bus.published]
    output_indices = [
        i for i, event_type in enumerate(types) if event_type is EventType.TOOL_OUTPUT
    ]
    completed_index = types.index(EventType.TOOL_COMPLETED)

    assert output_indices, "expected tool.output events"
    # Order matters: this proves live streaming, not a final dump after completion.
    assert max(output_indices) < completed_index

    chunks = "".join(
        event.data["chunk"] for event in bus.published if event.type is EventType.TOOL_OUTPUT
    )
    assert "hello-stream" in chunks

    first = bus.published[output_indices[0]]
    assert first.data["tool"] == "run_command"
    assert first.data["tool_call_id"] == "call-1"
    assert first.data["stream"] == "stdout"


async def test_command_emits_chunk_while_process_runs(tmp_workspace: Path) -> None:
    """A first chunk arrives while the command is still alive (not after it exits)."""
    first_chunk = asyncio.Event()
    seen: list[tuple[str, str]] = []

    async def on_output(stream: str, text: str) -> None:
        seen.append((stream, text))
        first_chunk.set()

    task = asyncio.create_task(
        run_command(
            ["/bin/sh", "-c", "echo first; sleep 1; echo second"],
            cwd=tmp_workspace,
            on_output=on_output,
        )
    )
    await asyncio.wait_for(first_chunk.wait(), timeout=5.0)

    assert not task.done(), "the first chunk should arrive before the command exits"

    result = await task
    assert result.stdout.splitlines() == ["first", "second"]
    assert [chunk for stream, chunk in seen if stream == "stdout"]


async def test_command_output_events_are_bounded(tmp_workspace: Path) -> None:
    """Emitting stops once the per-stream cap is reached, even if more output follows."""
    cap = 100
    emitted: list[str] = []

    async def on_output(stream: str, text: str) -> None:
        emitted.append(text)

    result = await run_command(
        ["/bin/sh", "-c", "yes aaa | head -c 100000"],
        cwd=tmp_workspace,
        max_stdout_bytes=cap,
        on_output=on_output,
    )

    total = sum(len(text) for text in emitted)
    assert 0 < total <= cap + 4096  # bounded to ~one read past the cap
    assert result.stdout  # the full (capped) output is still returned


async def test_command_cancellation_kills_process_group(tmp_workspace: Path) -> None:
    """Cancelling the run kills the whole child process group, not just the shell."""
    pid_file = tmp_workspace / "pid.txt"
    task = asyncio.create_task(
        run_command(
            ["/bin/sh", "-c", "echo $$ > pid.txt; exec sleep 30"],
            cwd=tmp_workspace,
        )
    )
    for _ in range(100):
        if pid_file.exists():
            break
        await asyncio.sleep(0.02)
    pgid = int(pid_file.read_text().strip())

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)  # the group is gone
