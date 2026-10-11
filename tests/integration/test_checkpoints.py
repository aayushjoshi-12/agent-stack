"""Integration tests for P10: run checkpoints and idempotent tool executions.

Deterministic fakes only (``FakeChatModel`` + duck-typed counting tools); the real
SQLite checkpoint store is exercised. The crash/resume scenario simulates a process
dying mid-loop: run 1 completes a tool batch then dies before its next model call;
run 2 (same run/session) skips the already-recorded tool call ids.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest
from langchain_core.messages import ToolMessage

from agent_stack.core.agent import Agent
from agent_stack.core.limits import AgentLimits
from agent_stack.core.runtime import AgentRuntime
from agent_stack.core.session import InMemorySessionStore
from agent_stack.core.state import Checkpoint, CheckpointStatus
from agent_stack.persistence.sqlite import SqliteCheckpointStore, SqliteDatabase
from agent_stack.tools.executor import ToolExecutionResult, ToolExecutor
from agent_stack.tools.registry import ToolRegistry
from tests.fakes import FakeChatModel, ai, call


class CountingTool:
    """A tool that counts its own invocations."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"Counts invocations of {name}."
        self.invocations = 0

    async def ainvoke(self, args: dict[str, Any]) -> str:
        self.invocations += 1
        return f"{self.name} ran {self.invocations}x"


class CountingConnection(sqlite3.Connection):
    """A connection that counts its commits (proves transaction granularity)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1
        super().commit()


def _runtime(model: Any, tools: list[Any], store: InMemorySessionStore) -> AgentRuntime:
    registry = ToolRegistry(tools)  # type: ignore[arg-type]
    executor = ToolExecutor(registry)
    agent = Agent(
        name="assistant",
        model="test:model",
        tools=tuple(tools),  # type: ignore[arg-type]
        system_prompt="You are a test assistant.",
        limits=AgentLimits(),
    )
    return AgentRuntime(
        agent=agent,
        model=model,
        tool_executor=executor,
        session_store=store,
    )


def _result(
    tool_call_id: str, tool_name: str, *, success: bool, error: str | None = None
) -> ToolExecutionResult:
    content = f"ok: {tool_call_id}" if success else "Error: boom"
    return ToolExecutionResult(
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        message=ToolMessage(
            content=content,
            tool_call_id=tool_call_id,
            status="success" if success else "error",
        ),
        success=success,
        error=error,
    )


@pytest.mark.integration
async def test_checkpoint_store_round_trips_latest(tmp_path: Any) -> None:
    store = SqliteCheckpointStore(SqliteDatabase(tmp_path / "index.db"))

    await store.save(
        Checkpoint(
            run_id="r1",
            session_id="s1",
            step=2,
            tool_call_count=1,
            status=CheckpointStatus.TOOL_BATCH_COMPLETED,
            created_at="t1",
        )
    )
    await store.save(
        Checkpoint(
            run_id="r1",
            session_id="s1",
            step=3,
            tool_call_count=1,
            status=CheckpointStatus.RUN_COMPLETED,
            created_at="t2",
        )
    )

    latest = await store.get_latest("r1")
    assert latest is not None
    assert latest.status == CheckpointStatus.RUN_COMPLETED
    assert latest.step == 3
    assert latest.tool_call_count == 1
    assert latest.session_id == "s1"
    assert await store.get_latest("missing") is None


@pytest.mark.integration
async def test_record_tool_executions_is_idempotent_and_records_status(tmp_path: Any) -> None:
    db = SqliteDatabase(tmp_path / "index.db")
    store = SqliteCheckpointStore(db)
    results = [
        _result("c1", "ok", success=True),
        _result("c2", "bad", success=False, error="boom"),
        _result("c3", "ok", success=True),
    ]

    await store.record_tool_executions("r1", results)
    await store.record_tool_executions("r1", results)  # same batch again: no-op

    assert await store.completed_tool_call_ids("r1") == {"c1", "c2", "c3"}
    rows = await db.query(
        "SELECT tool_call_id, status, error FROM tool_executions WHERE run_id = ? ORDER BY tool_call_id",
        ("r1",),
    )
    assert [(row["tool_call_id"], row["status"]) for row in rows] == [
        ("c1", "success"),
        ("c2", "error"),
        ("c3", "success"),
    ]
    assert rows[1]["error"] == "boom"
    assert rows[0]["error"] is None


@pytest.mark.integration
async def test_tool_batch_persisted_in_one_transaction(tmp_path: Any) -> None:
    """A whole tool batch lands in exactly one SQLite commit."""
    db = SqliteDatabase(tmp_path / "index.db", factory=CountingConnection)
    store = SqliteCheckpointStore(db)
    conn = db._conn  # type: ignore[attr-defined]

    before = conn.commits
    await store.record_tool_executions(
        "r1", [_result("c1", "ok", success=True), _result("c2", "ok", success=True)]
    )

    assert conn.commits == before + 1
    rows = await db.query("SELECT COUNT(*) AS n FROM tool_executions WHERE run_id = ?", ("r1",))
    assert rows[0]["n"] == 2


@pytest.mark.integration
async def test_runtime_writes_checkpoints_at_boundaries(tmp_path: Any) -> None:
    db = SqliteDatabase(tmp_path / "index.db")
    checkpoints = SqliteCheckpointStore(db)
    store = InMemorySessionStore()
    runtime = _runtime(
        FakeChatModel([ai(tool_calls=[call("alpha", id="a1")]), ai("done")]),
        [CountingTool("alpha")],
        store,
    )

    result = await runtime.run(
        session_id="s1",
        user_id="u1",
        message="go",
        run_id="r1",
        checkpoint_store=checkpoints,
    )

    assert result.content == "done"
    latest = await checkpoints.get_latest("r1")
    assert latest is not None
    assert latest.status == CheckpointStatus.RUN_COMPLETED
    assert latest.step == 2
    assert latest.tool_call_count == 1
    rows = await db.query("SELECT status FROM checkpoints WHERE run_id = ? ORDER BY id", ("r1",))
    assert [row["status"] for row in rows] == [
        "ai_message",
        "tool_batch_completed",
        "ai_message",  # the final (tool-call-free) AI message is a boundary too
        "run_completed",
    ]
    assert await checkpoints.completed_tool_call_ids("r1") == {"a1"}


@pytest.mark.integration
async def test_does_not_duplicate_completed_tool(tmp_path: Any) -> None:
    store = InMemorySessionStore()
    checkpoints = SqliteCheckpointStore(SqliteDatabase(tmp_path / "index.db"))
    tool = CountingTool("count")
    runtime = _runtime(
        FakeChatModel([ai(tool_calls=[call("count", id="c1")]), ai("done")]),
        [tool],
        store,
    )

    # First run records the completed (run_id, tool_call_id).
    result = await runtime.run(
        session_id="s1",
        user_id="u1",
        message="go",
        run_id="r1",
        checkpoint_store=checkpoints,
    )
    assert result.content == "done"
    assert tool.invocations == 1

    # Re-running the same run id (fresh attempt after a crash) must skip the recorded
    # call, not re-execute it.
    resumed = _runtime(
        FakeChatModel([ai(tool_calls=[call("count", id="c1")]), ai("done")]),
        [tool],
        store,
    )
    result = await resumed.run(
        session_id="s1",
        user_id="u1",
        message="go",
        run_id="r1",
        checkpoint_store=checkpoints,
    )
    assert result.content == "done"
    assert tool.invocations == 1
    assert await checkpoints.completed_tool_call_ids("r1") == {"c1"}


@pytest.mark.integration
async def test_resumes_from_checkpoint(tmp_path: Any) -> None:
    """A run that crashed mid-loop resumes and never re-runs a completed tool."""
    store = InMemorySessionStore()
    db = SqliteDatabase(tmp_path / "index.db")
    checkpoints = SqliteCheckpointStore(db)
    tool_a = CountingTool("alpha")
    tool_b = CountingTool("beta")

    # Attempt 1: completes tool alpha, then "crashes" (model exhausted) before persisting.
    crashed = _runtime(
        FakeChatModel([ai(tool_calls=[call("alpha", id="a1")])]),
        [tool_a, tool_b],
        store,
    )
    with pytest.raises(AssertionError):
        await crashed.run(
            session_id="s1",
            user_id="u1",
            message="go",
            run_id="r1",
            checkpoint_store=checkpoints,
        )
    assert tool_a.invocations == 1
    assert tool_b.invocations == 0
    assert await store.get_messages("s1") == []  # nothing persisted before the crash

    # Attempt 2 (same run/session): alpha is skipped; beta runs once; run completes.
    resumed = _runtime(
        FakeChatModel(
            [
                ai(tool_calls=[call("alpha", id="a1")]),
                ai(tool_calls=[call("beta", id="b1")]),
                ai("done"),
            ]
        ),
        [tool_a, tool_b],
        store,
    )
    result = await resumed.run(
        session_id="s1",
        user_id="u1",
        message="go",
        run_id="r1",
        checkpoint_store=checkpoints,
    )

    assert result.content == "done"
    assert tool_a.invocations == 1  # never re-ran
    assert tool_b.invocations == 1

    messages = await store.get_messages("s1")
    assert [message.content for message in messages] == [
        "go",
        "",
        "(skipped: already completed in a previous attempt of this run)",
        "",
        "beta ran 1x",
        "done",
    ]
    assert isinstance(messages[2], ToolMessage)
    assert messages[2].tool_call_id == "a1"

    latest = await checkpoints.get_latest("r1")
    assert latest is not None
    assert latest.status == CheckpointStatus.RUN_COMPLETED
    assert latest.step == 3
    rows = await db.query("SELECT status FROM checkpoints WHERE run_id = ? ORDER BY id", ("r1",))
    assert [row["status"] for row in rows] == [
        "ai_message",
        "tool_batch_completed",
        "ai_message",
        "ai_message",
        "tool_batch_completed",
        "ai_message",
        "run_completed",
    ]
