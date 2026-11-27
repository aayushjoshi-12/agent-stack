"""P35: the per-call tool timeout is configurable, reports output so far, and kills commands."""

from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import ToolMessage

from agent_stack.app.config import AppConfig
from agent_stack.core.agent import Agent
from agent_stack.core.errors import ConfigurationError
from agent_stack.core.limits import AgentLimits
from agent_stack.core.runtime import AgentRuntime
from agent_stack.core.session import InMemorySessionStore
from agent_stack.core.tool_stream import current_tool_output_sink
from agent_stack.core.workspace import WorkspacePolicy
from agent_stack.tools.builtins.terminal import run_command_tool
from agent_stack.tools.executor import ToolExecutor
from agent_stack.tools.registry import ToolRegistry
from tests.fakes import FakeChatModel, SleepTool, ai, call


def _call(name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"name": name, "args": args or {}, "id": "t1", "type": "tool_call"}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class ChattyTool:
    """Streams a line, then hangs: the cut-off must still report the line."""

    name = "chatty"
    description = "Print then hang."

    async def ainvoke(self, args: dict[str, Any]) -> str:
        sink = current_tool_output_sink()
        assert sink is not None
        await sink("stdout", "halfway there\n")
        await asyncio.sleep(30)
        return "never"


def test_tool_timeout_config() -> None:
    assert AppConfig.from_env({}).tool_timeout_seconds == 300.0
    assert AppConfig.from_env({"AGENT_STACK_TOOL_TIMEOUT": "3"}).tool_timeout_seconds == 3.0
    for off in ("off", "0", "OFF"):
        assert AppConfig.from_env({"AGENT_STACK_TOOL_TIMEOUT": off}).tool_timeout_seconds is None
    for bad in ("soon", "-5"):
        with pytest.raises(ConfigurationError):
            AppConfig.from_env({"AGENT_STACK_TOOL_TIMEOUT": bad})


async def test_run_goes_on_after_a_tool_times_out() -> None:
    store = InMemorySessionStore()
    tools = (SleepTool("slow", 30.0),)
    agent = Agent(
        name="assistant",
        model="test:model",
        tools=tools,  # type: ignore[arg-type]
        limits=AgentLimits(tool_timeout_seconds=0.1),
    )
    runtime = AgentRuntime(
        agent=agent,
        model=FakeChatModel([ai(tool_calls=[call("slow", {})]), ai("carried on")]),
        tool_executor=ToolExecutor(
            ToolRegistry(tools),  # type: ignore[arg-type]
            timeout_seconds=agent.limits.tool_timeout_seconds,
        ),
        session_store=store,
    )

    started = time.monotonic()
    response = await runtime.run(session_id="s1", user_id="u1", message="go")

    assert time.monotonic() - started < 5.0
    assert response.content == "carried on"
    tool_message = next(m for m in await store.get_messages("s1") if isinstance(m, ToolMessage))
    assert "timed out after 0.1s" in str(tool_message.content)


async def test_timeout_result_carries_output_so_far() -> None:
    executor = ToolExecutor(ToolRegistry((ChattyTool(),)), timeout_seconds=0.1)  # type: ignore[arg-type]

    result = await executor.execute_one(_call("chatty"))

    assert result.success is False
    assert "timed out after 0.1s; output so far:\nhalfway there" in str(result.message.content)


async def test_run_command_past_the_cap_kills_its_process_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    executor = ToolExecutor(
        ToolRegistry((run_command_tool(WorkspacePolicy(tmp_path)),)), timeout_seconds=1.0
    )

    started = time.monotonic()
    result = await executor.execute_one(
        _call(
            "run_command",
            # the model asks for longer than the cap: the cap wins
            {
                "command": "echo started; sleep 999 & echo $! > pid; wait",
                "shell": True,
                "timeout": 999,
            },
        )
    )
    elapsed = time.monotonic() - started

    assert elapsed < 4.0  # the cap plus the SIGTERM grace, not 999 s
    content = str(result.message.content)
    assert re.search(r"timed out after 1s; output so far:\nstarted", content), content
    sleeper = int(pid_file.read_text())
    for _ in range(50):  # reaping is asynchronous; give the kernel a moment
        if not _alive(sleeper):
            break
        await asyncio.sleep(0.05)
    assert not _alive(sleeper), "the backgrounded sleep outlived the cut-off"


async def test_no_cap_when_off() -> None:
    executor = ToolExecutor(ToolRegistry((SleepTool("nap", 0.3),)), timeout_seconds=None)  # type: ignore[arg-type]

    result = await executor.execute_one(_call("nap"))

    assert result.success is True


async def test_a_tool_can_lift_the_cap() -> None:
    tool = SleepTool("long", 0.3)
    tool.metadata = {"timeout_seconds": None}  # type: ignore[attr-defined]
    executor = ToolExecutor(ToolRegistry((tool,)), timeout_seconds=0.05)  # type: ignore[arg-type]

    result = await executor.execute_one(_call("long"))

    assert result.success is True


def test_delegate_is_bounded_by_the_subagent_not_the_cap() -> None:
    from agent_stack.subagents.delegation import delegate

    assert delegate.metadata == {"timeout_seconds": None}
