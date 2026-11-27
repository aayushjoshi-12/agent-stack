"""P54: a tool that ran is never hidden from the model, so a retry does not run it again.

The app never re-runs a run id (a retry is a new run), so what keeps a mutating tool from
running twice is the transcript: the call and its result are written before anything can fail,
and a call that died mid-flight reaches the next run as "outcome unknown".
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent_stack.core.runtime import answer_dangling_calls
from agent_stack.core.session import InMemorySessionStore
from agent_stack.persistence.sqlite import SqliteCheckpointStore, SqliteDatabase
from tests.fakes import FakeChatModel, ai, call
from tests.integration.test_checkpoints import CountingTool, _runtime


class FailingRecords(SqliteCheckpointStore):
    """Recording the tool batch fails: the tools already ran."""

    async def record_tool_executions(self, run_id: str, results: Any) -> None:
        raise OSError("disk full")


def _tool_results(messages: list[Any]) -> list[str]:
    return [str(m.content) for m in messages if isinstance(m, ToolMessage)]


@pytest.mark.integration
async def test_recording_failure_keeps_the_result_for_the_next_run(tmp_path: Any) -> None:
    store = InMemorySessionStore()
    tool = CountingTool("append")
    runtime = _runtime(FakeChatModel([ai(tool_calls=[call("append", id="c1")])]), [tool], store)

    with pytest.raises(OSError):
        await runtime.run(
            session_id="s1",
            user_id="u1",
            message="append a line",
            run_id="r1",
            checkpoint_store=FailingRecords(SqliteDatabase(tmp_path / "index.db")),
        )
    assert tool.invocations == 1

    retry_model = FakeChatModel([ai("It is already there.")])
    retry = _runtime(retry_model, [tool], store)
    await retry.run(session_id="s1", user_id="u1", message="try again", run_id="r2")

    seen = retry_model.calls[0]
    assert _tool_results(seen) == ["append ran 1x"]  # the model was told it ran
    assert tool.invocations == 1


class BlockingTool(CountingTool):
    """Starts its side effect, then never finishes (the run dies while it runs)."""

    async def ainvoke(self, args: dict[str, Any]) -> str:
        self.invocations += 1
        await asyncio.Event().wait()
        return "unreachable"


@pytest.mark.integration
async def test_a_call_cut_off_mid_flight_reaches_the_next_run_as_unknown() -> None:
    store = InMemorySessionStore()
    tool = BlockingTool("write")
    runtime = _runtime(FakeChatModel([ai(tool_calls=[call("write", id="c1")])]), [tool], store)
    task = asyncio.create_task(
        runtime.run(session_id="s1", user_id="u1", message="write it", run_id="r1")
    )
    while tool.invocations == 0:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    retry_model = FakeChatModel([ai("checked")])
    await _runtime(retry_model, [tool], store).run(
        session_id="s1", user_id="u1", message="try again", run_id="r2"
    )

    [unknown] = _tool_results(retry_model.calls[0])
    assert "outcome unknown" in unknown
    assert tool.invocations == 1


def test_answer_dangling_calls_fills_in_place() -> None:
    crashed = [
        HumanMessage(content="go"),
        AIMessage(content="", tool_calls=[call("a", id="a1"), call("b", id="b1")]),
        ToolMessage(content="a done", tool_call_id="a1"),
        HumanMessage(content="next turn"),
    ]

    repaired = answer_dangling_calls(crashed)

    assert [type(m).__name__ for m in repaired] == [
        "HumanMessage",
        "AIMessage",
        "ToolMessage",
        "ToolMessage",
        "HumanMessage",
    ]
    assert repaired[3].tool_call_id == "b1"  # type: ignore[union-attr]
    assert "outcome unknown" in str(repaired[3].content)
    assert answer_dangling_calls(repaired) == repaired  # a whole history is left alone
