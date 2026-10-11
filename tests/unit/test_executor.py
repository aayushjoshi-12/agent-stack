"""Unit tests for :class:`agent_stack.tools.executor.ToolExecutor`."""

from __future__ import annotations

import time

from agent_stack.tools.executor import ToolExecutor
from agent_stack.tools.registry import ToolRegistry
from tests.fakes import FailingTool, SleepTool


class _RecordingTool:
    """Records the ``args`` it receives so we can prove what was passed in."""

    def __init__(self, name: str, result: str = "ok") -> None:
        self.name = name
        self.description = "Records args."
        self._result = result
        self.received: list[object] = []

    async def ainvoke(self, args):
        self.received.append(args)
        return self._result


def _call(name: str, args: dict | None = None, id: str = "call-1") -> dict:
    return {"name": name, "args": args or {}, "id": id, "type": "tool_call"}


async def test_passes_args_not_tool_call() -> None:
    """Regression: the executor must invoke with ``args``, not the whole call."""
    tool = _RecordingTool("rec")
    executor = ToolExecutor(ToolRegistry((tool,)))
    tool_call = _call("rec", {"x": 1}, id="c1")

    await executor.execute_one(tool_call)

    assert tool.received == [{"x": 1}]
    assert tool.received[0] != tool_call


async def test_returns_result_with_tool_message() -> None:
    tool = _RecordingTool("rec", result=42)
    executor = ToolExecutor(ToolRegistry((tool,)))

    result = await executor.execute_one(_call("rec", {}, id="abc"))

    assert result.tool_name == "rec"
    assert result.success is True
    assert result.error is None
    assert result.message.content == "42"
    assert result.message.tool_call_id == "abc"


async def test_tool_error_becomes_tool_message() -> None:
    tool = FailingTool(name="boom", message="kaboom")
    executor = ToolExecutor(ToolRegistry((tool,)))

    result = await executor.execute_one(_call("boom", {}, id="e1"))

    assert result.success is False
    assert result.error == "kaboom"
    assert result.message.status == "error"
    assert "kaboom" in result.message.content
    assert result.message.tool_call_id == "e1"


async def test_unknown_tool_becomes_tool_message() -> None:
    executor = ToolExecutor(ToolRegistry((SleepTool("known", 0.0),)))

    result = await executor.execute_one(_call("nope", {}, id="u1"))

    assert result.success is False
    assert result.message.status == "error"
    assert "Unknown tool" in result.message.content
    assert result.message.tool_call_id == "u1"


async def test_execute_many_preserves_order() -> None:
    first = _RecordingTool("first", result="one")
    second = _RecordingTool("second", result="two")
    executor = ToolExecutor(ToolRegistry((first, second)))

    results = await executor.execute_many([_call("second", id="c2"), _call("first", id="c1")])

    assert [result.message.tool_call_id for result in results] == ["c2", "c1"]
    assert [result.message.content for result in results] == ["two", "one"]
    assert [result.tool_name for result in results] == ["second", "first"]


async def test_execute_many_empty_returns_empty() -> None:
    executor = ToolExecutor(ToolRegistry(()))

    assert await executor.execute_many([]) == []


async def test_execute_many_runs_concurrently() -> None:
    executor = ToolExecutor(ToolRegistry((SleepTool("slow-a", 0.25), SleepTool("slow-b", 0.25))))

    start = time.perf_counter()
    results = await executor.execute_many([_call("slow-a", id="a"), _call("slow-b", id="b")])
    elapsed = time.perf_counter() - start

    # Sequential execution would take >= 0.5s; concurrent should be ~0.25s.
    assert len(results) == 2
    assert elapsed < 0.45
