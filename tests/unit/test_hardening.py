"""P28: the ugly paths: failures keep a consistent history, secrets and floods stay out of the
model's context, and deleting a session leaves nothing behind."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent_stack.core.agent import Agent
from agent_stack.core.errors import RuntimeDeadlineExceeded
from agent_stack.core.event_bus import InMemoryEventBus
from agent_stack.core.events import AgentEvent, EventType
from agent_stack.core.limits import AgentLimits
from agent_stack.core.run import RunManager, RunRequest, RunStatus
from agent_stack.core.runtime import AgentRuntime
from agent_stack.core.session import InMemorySessionStore, Session
from agent_stack.memory.store import SqliteMemoryStore
from agent_stack.persistence.conversation import FileConversationStore
from agent_stack.persistence.memory import InMemoryRunStore
from agent_stack.persistence.sqlite import SqliteDatabase, SqliteSessionStore
from agent_stack.persistence.todos import SqliteTodoStore
from agent_stack.tools.executor import MAX_OUTPUT_BYTES, ToolExecutor
from agent_stack.tools.registry import ToolRegistry
from tests.fakes import FakeChatModel, SleepTool, ai, call


class EchoTool:
    name = "echo"
    description = "echo"

    async def ainvoke(self, args: dict[str, Any]) -> str:
        return "echoed"


class RaisingModel:
    """A provider that fails every call with ``error`` (a timeout, a 429, ...)."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def bind_tools(self, tools: Any) -> RaisingModel:
        return self

    async def ainvoke(self, messages: Any, **kwargs: Any) -> AIMessage:
        raise self._error


def _runtime(
    model: Any,
    tools: list[Any] | None = None,
    *,
    store: Any = None,
    bus: InMemoryEventBus | None = None,
    limits: AgentLimits | None = None,
) -> AgentRuntime:
    tools = tools or []
    agent = Agent(name="assistant", model="m", tools=tuple(tools), limits=limits or AgentLimits())
    return AgentRuntime(
        agent=agent,
        model=model,
        tool_executor=ToolExecutor(ToolRegistry(tools)),
        session_store=store or InMemorySessionStore(),
        event_bus=bus,
    )


def _dangling_tool_calls(messages: list[Any]) -> list[str]:
    """Tool calls with no matching ToolMessage: what a provider rejects on the next turn."""
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    return [
        c["id"]
        for m in messages
        if isinstance(m, AIMessage)
        for c in m.tool_calls
        if c["id"] not in answered
    ]


# ── a failed or cancelled run keeps a consistent history ─────────────────────
@pytest.mark.parametrize(
    "error",
    [TimeoutError("provider timed out"), RuntimeError("429 Too Many Requests")],
    ids=["provider-timeout", "provider-429"],
)
async def test_a_provider_failure_keeps_the_users_message_and_the_session_usable(
    error: Exception,
) -> None:
    store, bus = InMemorySessionStore(), InMemoryEventBus()
    events: list[AgentEvent] = []

    class Recording(InMemoryEventBus):
        async def publish(self, event: AgentEvent) -> None:
            events.append(event)
            await super().publish(event)

    bus = Recording()
    failing = _runtime(RaisingModel(error), store=store, bus=bus)

    with pytest.raises(type(error), match=str(error)):
        await failing.run(session_id="s", user_id="u", message="please help")

    assert [m.content for m in await store.get_messages("s")] == ["please help"]
    failed = [e for e in events if e.type is EventType.RUN_FAILED]
    assert len(failed) == 1 and str(error) in failed[0].data["error"]

    recovered = _runtime(FakeChatModel([ai("back again")]), store=store)  # provider is back
    result = await recovered.run(session_id="s", user_id="u", message="retry")
    assert result.content == "back again"
    assert [type(m).__name__ for m in await store.get_messages("s")] == [
        "HumanMessage",
        "HumanMessage",
        "AIMessage",
    ]


async def test_a_limit_failure_saves_only_a_consistent_prefix() -> None:
    store = InMemorySessionStore()
    model = FakeChatModel(
        [
            ai(tool_calls=[call("echo", id="c1")]),
            ai(tool_calls=[call("slow", id="c2")]),  # cut off by the deadline: never finished
        ]
    )
    limits = AgentLimits(max_runtime_seconds=0.3, tool_timeout_seconds=30.0)
    runtime = _runtime(model, [EchoTool(), SleepTool("slow", 5.0)], store=store, limits=limits)

    with pytest.raises(RuntimeDeadlineExceeded):
        await runtime.run(session_id="s", user_id="u", message="go")

    messages = await store.get_messages("s")
    assert [type(m).__name__ for m in messages] == ["HumanMessage", "AIMessage", "ToolMessage"]
    assert _dangling_tool_calls(messages) == []  # c2's assistant message was not kept


async def test_a_cancelled_run_keeps_the_message_and_no_dangling_tool_call() -> None:
    store, bus = InMemorySessionStore(), InMemoryEventBus()
    runtime = _runtime(
        FakeChatModel([ai(tool_calls=[call("slow", id="s1")]), ai("never")]),
        [SleepTool("slow", 5.0)],
        store=store,
        bus=bus,
    )
    manager = RunManager(runtime=runtime, run_store=InMemoryRunStore(), event_bus=bus)

    handle = await manager.start(RunRequest(session_id="s", user_id="u", message="do it"))
    await asyncio.sleep(0.1)
    await manager.cancel(handle.run_id)

    messages = await store.get_messages("s")
    assert [m.content for m in messages] == ["do it"]
    assert _dangling_tool_calls(messages) == []
    run = await manager.get_run(handle.run_id)
    assert run is not None and run.status is RunStatus.CANCELLED


async def test_failing_to_save_never_hides_the_real_error() -> None:
    class BrokenStore(InMemorySessionStore):
        async def append_messages(self, session_id: str, messages: Any) -> None:
            raise OSError("disk full")

    runtime = _runtime(RaisingModel(RuntimeError("provider down")), store=BrokenStore())

    with pytest.raises(RuntimeError, match="provider down"):  # not the OSError
        await runtime.run(session_id="s", user_id="u", message="hi")


# ── what the model is allowed to see of a tool's result ──────────────────────
class _Output:
    def __init__(self, text: str) -> None:
        self.name = "emit"
        self.description = "emit"
        self._text = text

    async def ainvoke(self, args: dict[str, Any]) -> str:
        return self._text


async def _tool_text(text: str, monkeypatch: pytest.MonkeyPatch | None = None) -> str:
    executor = ToolExecutor(ToolRegistry([_Output(text)]))  # type: ignore[arg-type]
    result = await executor.execute_one({"name": "emit", "args": {}, "id": "c"})
    return str(result.message.content)


async def test_tool_output_has_known_secrets_and_key_shapes_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEMO_API_TOKEN", "zebra-quartz-9921!")
    text = await _tool_text(
        "OPENROUTER=sk-or-v1-0123456789abcdef0123456789abcdef and pw zebra-quartz-9921!"
    )

    assert "sk-or-v1" not in text and "zebra-quartz" not in text
    assert text.count("[redacted]") == 2


async def test_tool_output_keeps_ordinary_code_the_agent_must_read() -> None:
    code = "def auth(token):\n    password = get_password()\n    return token_for(password)"

    assert await _tool_text(code) == code  # a variable named password is not a leak


async def test_an_error_message_is_redacted_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_SECRET", "hunter2-hunter2")

    class Leaky:
        name = "leak"
        description = "leak"

        async def ainvoke(self, args: dict[str, Any]) -> str:
            raise RuntimeError("login failed for hunter2-hunter2")

    executor = ToolExecutor(ToolRegistry([Leaky()]))  # type: ignore[arg-type]
    result = await executor.execute_one({"name": "leak", "args": {}, "id": "c"})

    assert result.success is False
    assert "hunter2" not in str(result.message.content)


async def test_huge_tool_output_is_capped_before_it_reaches_the_model() -> None:
    text = await _tool_text("x" * 5_000_000)

    assert len(text.encode()) <= MAX_OUTPUT_BYTES
    assert "truncated" in text.lower()
    assert await _tool_text("short") == "short"  # small results are untouched


# ── deleting a session leaves nothing behind ─────────────────────────────────
async def test_deleting_a_session_leaves_no_orphans_anywhere(tmp_path: Path) -> None:
    db = SqliteDatabase(tmp_path / "agent.db")
    conversation = FileConversationStore(tmp_path / "agent")
    sessions = SqliteSessionStore(db, conversation)
    todos, memory = SqliteTodoStore(db), SqliteMemoryStore(db)
    for sid in ("gone", "kept"):
        await sessions.save(Session(id=sid, owner_id="u", agent_name="a"))
        await sessions.append_messages(sid, [HumanMessage(content=f"hello {sid}")])
        await todos.add(sid, f"todo for {sid}")
        await db.execute(
            "INSERT INTO runs (id, session_id, status) VALUES (?, ?, 'completed')",
            (f"run-{sid}", sid),
        )
        await db.execute(
            "INSERT INTO message_index (session_id, run_id, seq, kind, jsonl_offset, created_at)"
            " VALUES (?, ?, 0, 'human', 0, 'now')",
            (sid, f"run-{sid}"),
        )
        await db.execute(
            "INSERT INTO artifacts_index (session_id, run_id, name, rel_path, size_bytes,"
            " created_at) VALUES (?, ?, 'a', 'a', 1, 'now')",
            (sid, f"run-{sid}"),
        )
        await db.execute(
            "INSERT INTO checkpoints (run_id, session_id, step, tool_call_count, status,"
            " created_at) VALUES (?, ?, 1, 0, 'ai_message', 'now')",
            (f"run-{sid}", sid),
        )
        await db.execute(
            "INSERT INTO tool_executions (run_id, tool_call_id, tool_name, status, ts)"
            " VALUES (?, 'c1', 't', 'ok', 'now')",
            (f"run-{sid}",),
        )
    await memory.remember("user:u", "city", "Haridwar")

    await sessions.delete("gone")

    for table in ("sessions", "runs", "message_index", "artifacts_index", "checkpoints", "todos"):
        column = "id" if table == "sessions" else "session_id"
        gone = await db.query(f"SELECT 1 FROM {table} WHERE {column} = 'gone'")
        kept = await db.query(f"SELECT 1 FROM {table} WHERE {column} = 'kept'")
        assert gone == [] and len(kept) == 1, table
    assert await db.query("SELECT 1 FROM tool_executions WHERE run_id = 'run-gone'") == []
    assert len(await db.query("SELECT 1 FROM tool_executions WHERE run_id = 'run-kept'")) == 1
    assert not conversation.session_dir("gone").exists()  # the transcript files are gone too
    assert conversation.session_dir("kept").exists()
    # Facts are the user's, not the session's: they deliberately outlive the conversation.
    assert (await memory.get("user:u", "city")) is not None


# ── a long conversation never reaches the model as a broken window ───────────
def _tool_conversation(turns: int) -> list[Any]:
    """Human, assistant-with-tool-call, tool result, assistant: repeated ``turns`` times."""
    history: list[Any] = []
    for i in range(turns):
        history += [
            HumanMessage(content=f"q{i}"),
            AIMessage(content="", tool_calls=[call("t", id=f"c{i}")]),
            ToolMessage(content="result", tool_call_id=f"c{i}"),
            AIMessage(content=f"a{i}"),
        ]
    return history


def test_the_window_never_starts_on_a_tool_result_whose_call_was_cut_off() -> None:
    """Regression: some history lengths cut between a tool call and its result, which
    providers reject ("tool message without a preceding tool call")."""
    from agent_stack.core.context import ContextBuilder

    builder = ContextBuilder(recent_window=40)
    history = _tool_conversation(30)

    for end in range(1, len(history) + 1):
        window = builder.build(system_parts=["s"], history=history[:end]).messages
        assert not window or not isinstance(window[0], ToolMessage), f"history[:{end}]"
        called = {c["id"] for m in window if isinstance(m, AIMessage) for c in m.tool_calls}
        assert all(m.tool_call_id in called for m in window if isinstance(m, ToolMessage))


async def test_a_very_long_conversation_reaches_the_model_bounded_and_fast() -> None:
    import time

    from agent_stack.core.context import ContextBuilder

    store = InMemorySessionStore()
    await store.append_messages("s", _tool_conversation(1500))  # 6,000 messages of history
    model = FakeChatModel([ai("ok")])
    agent = Agent(name="assistant", model="m")
    runtime = AgentRuntime(
        agent=agent,
        model=model,
        tool_executor=ToolExecutor(ToolRegistry([])),
        session_store=store,
        context_builder=ContextBuilder(recent_window=40),
    )

    started = time.monotonic()
    await runtime.run(session_id="s", user_id="u", message="latest question")

    sent = model.calls[0]
    assert len(sent) <= 42  # one system message + the recent window (+ the new message)
    assert not isinstance(sent[1], ToolMessage)
    assert sent[-1].content == "latest question"
    assert time.monotonic() - started < 2.0


# ── the database failing underneath a run ────────────────────────────────────
async def test_a_failing_checkpoint_database_fails_the_run_cleanly() -> None:
    import sqlite3

    class LockedStore:
        async def save(self, checkpoint: Any) -> None:
            raise sqlite3.OperationalError("database is locked")

        async def completed_tool_call_ids(self, run_id: str) -> set[str]:
            return set()

        async def record_tool_executions(self, run_id: str, results: Any) -> None:
            raise sqlite3.OperationalError("database is locked")

    store = InMemorySessionStore()
    events: list[AgentEvent] = []

    class Recording(InMemoryEventBus):
        async def publish(self, event: AgentEvent) -> None:
            events.append(event)
            await super().publish(event)

    runtime = _runtime(FakeChatModel([ai("hi")]), store=store, bus=Recording())

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        await runtime.run(
            session_id="s",
            user_id="u",
            message="hello",
            run_id="r1",
            checkpoint_store=LockedStore(),
        )

    assert [e.type for e in events if e.type is EventType.RUN_FAILED] == [EventType.RUN_FAILED]
    assert [m.content for m in await store.get_messages("s")][:1] == ["hello"]  # nothing lost
    again = _runtime(FakeChatModel([ai("fine")]), store=store)  # the next run is unaffected
    assert (await again.run(session_id="s", user_id="u", message="again")).content == "fine"


# ── a subagent that fails or times out does not take its parent down ─────────
async def _delegate_to(
    child_model: Any, child_limits: AgentLimits | None = None, tools: Any = ()
) -> str:
    from agent_stack.core.agent import SubAgentSpec
    from agent_stack.subagents.delegation import delegate
    from tests.unit.test_subagents import FakeFactory

    store, bus = InMemorySessionStore(), InMemoryEventBus()
    child = Agent(
        name="explorer",
        model="m",
        tools=tuple(tools),
        limits=child_limits or AgentLimits(),
    )
    child_runtime = AgentRuntime(
        agent=child,
        model=child_model,
        tool_executor=ToolExecutor(ToolRegistry(list(tools))),
        session_store=store,
        event_bus=bus,
    )
    parent = Agent(
        name="assistant",
        model="m",
        tools=(delegate,),
        subagents=(SubAgentSpec("explorer", "explore", child),),
    )
    parent_runtime = AgentRuntime(
        agent=parent,
        model=FakeChatModel(
            [
                ai(
                    tool_calls=[
                        call("delegate", {"agent_name": "explorer", "task": "look"}, id="d1")
                    ]
                ),
                ai("parent finished"),
            ]
        ),
        tool_executor=ToolExecutor(ToolRegistry([delegate])),  # type: ignore[list-item]
        session_store=store,
        event_bus=bus,
    )
    manager = RunManager(
        runtime=parent_runtime,
        run_store=InMemoryRunStore(),
        event_bus=bus,
        agent_factory=FakeFactory({"explorer": child_runtime}),  # type: ignore[arg-type]
    )

    handle = await manager.start(RunRequest(session_id="s", user_id="u", message="go"))
    result = await asyncio.wait_for(handle.result(), 5.0)
    assert result.content == "parent finished"  # the parent carried on regardless
    (tool_message,) = [m for m in await store.get_messages("s") if isinstance(m, ToolMessage)]
    return str(tool_message.content)


async def test_a_failing_subagent_is_reported_to_the_parent_not_raised() -> None:
    reported = await _delegate_to(RaisingModel(RuntimeError("explorer exploded")))

    assert "explorer exploded" in reported


async def test_a_subagent_that_times_out_is_reported_to_the_parent() -> None:
    reported = await _delegate_to(
        FakeChatModel([ai(tool_calls=[call("slow", id="s1")]), ai("found half of it")]),
        AgentLimits(max_runtime_seconds=0.1, tool_timeout_seconds=30.0),
        tools=[SleepTool("slow", 5.0)],
    )

    # P48: cut off mid-tool, it still gets one call to report what it found
    assert "stopped at the time limit" in reported
    assert "found half of it" in reported


async def test_a_subagent_that_times_out_and_cannot_report_is_reported_as_failed() -> None:
    reported = await _delegate_to(
        FakeChatModel([ai(tool_calls=[call("slow", id="s1")])]),  # nothing left to answer
        AgentLimits(max_runtime_seconds=0.1, tool_timeout_seconds=30.0),
        tools=[SleepTool("slow", 5.0)],
    )

    assert "runtime limit" in reported
