"""P45 token streaming: the runtime streams the model, publishes coalesced deltas, and still
emits the final message, reasoning and usage exactly as before. Deltas are ephemeral."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk

from agent_stack.core.agent import Agent
from agent_stack.core.cancellation import CancellationToken
from agent_stack.core.errors import RunCancelled, RuntimeDeadlineExceeded
from agent_stack.core.event_bus import InMemoryEventBus
from agent_stack.core.events import AgentEvent, EventType
from agent_stack.core.limits import AgentLimits
from agent_stack.core.runtime import AgentRuntime
from agent_stack.core.session import InMemorySessionStore
from agent_stack.persistence.event_log import EventStoreBus, SqliteEventLogStore
from agent_stack.persistence.sqlite import SqliteDatabase
from agent_stack.tools.executor import ToolExecutor
from agent_stack.tools.registry import ToolRegistry
from tests.fakes import FakeChatModel, FakeStreamingModel, ai


class Recorder(InMemoryEventBus):
    """A real bus with an audit sink that records what it is given, plus a live subscriber."""

    def __init__(self, audit: Any = None) -> None:
        self.audited: list[AgentEvent] = []
        self.delivered: list[AgentEvent] = []
        super().__init__(audit=audit or self)

    def write(self, event: AgentEvent) -> None:  # the audit sink
        self.audited.append(event)

    async def publish(self, event: AgentEvent) -> None:
        await super().publish(event)
        self.delivered.append(event)  # as published (the bus assigns sequences to its copy)


class EchoTool:
    name = "echo"
    description = "Echo the arguments back."

    async def ainvoke(self, args: dict[str, Any]) -> str:
        return f"echo: {args}"


def _runtime(model: Any, bus: Any, *, limits: AgentLimits | None = None) -> AgentRuntime:
    tools = [EchoTool()]
    return AgentRuntime(
        agent=Agent(
            name="assistant",
            model="test:model",
            tools=tuple(tools),  # type: ignore[arg-type]
            system_prompt="t",
            limits=limits or AgentLimits(),
        ),
        model=model,
        tool_executor=ToolExecutor(ToolRegistry(tools)),  # type: ignore[arg-type]
        session_store=InMemorySessionStore(),
        event_bus=bus,
    )


def _text(*pieces: str) -> list[AIMessageChunk]:
    return [AIMessageChunk(content=p) for p in pieces]


USAGE = {
    "input_tokens": 100,
    "output_tokens": 20,
    "total_tokens": 120,
    "input_token_details": {"cache_read": 64, "cache_creation": 0},
    "output_token_details": {"reasoning": 5},
}


def _reply_with_reasoning_and_usage() -> list[AIMessageChunk]:
    return [
        AIMessageChunk(content="", additional_kwargs={"reasoning_content": "let me "}),
        AIMessageChunk(content="", additional_kwargs={"reasoning_content": "think"}),
        AIMessageChunk(content="Hello"),
        AIMessageChunk(content=", world"),
        AIMessageChunk(
            content="",
            usage_metadata=USAGE,  # type: ignore[arg-type]
            response_metadata={"cost": 0.001, "model_name": "m"},
        ),
    ]


def _deltas(bus: Recorder) -> list[AgentEvent]:
    return [e for e in bus.delivered if e.type is EventType.ASSISTANT_DELTA]


async def _subscribe(bus: InMemoryEventBus, run_id: str, into: list[AgentEvent]) -> asyncio.Task:
    async def consume() -> None:
        async for event in bus.subscribe(run_id):
            into.append(event)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    return task


async def test_deltas_stream_in_order_and_the_final_events_stay_the_same() -> None:
    bus = Recorder()
    model = FakeStreamingModel([_reply_with_reasoning_and_usage()])
    seen: list[AgentEvent] = []
    listener = await _subscribe(bus, "r1", seen)

    reply = await _runtime(model, bus).run(session_id="s", user_id="u", message="hi", run_id="r1")
    await listener

    deltas = [(e.data["kind"], e.data["text"], e.data["step"]) for e in seen if e.sequence == -1]
    # the first piece goes out at once; the rest gathers until the kind changes or it ends
    assert deltas == [
        ("reasoning", "let me ", 0),
        ("reasoning", "think", 0),
        ("text", "Hello, world", 0),
    ]
    assert [e.type for e in seen if e.sequence >= 0] == [
        EventType.RUN_STARTED,
        EventType.MODEL_USAGE,
        EventType.MODEL_REASONING,
        EventType.ASSISTANT_MESSAGE,
        EventType.RUN_COMPLETED,
    ]
    # deltas come before the step's final events, and the durable sequences have no gaps
    assert seen.index(next(e for e in seen if e.sequence == -1)) < seen.index(
        next(e for e in seen if e.type is EventType.ASSISTANT_MESSAGE)
    )
    assert [e.sequence for e in seen if e.sequence >= 0] == [0, 1, 2, 3, 4]

    assert isinstance(reply, AIMessage) and not isinstance(reply, AIMessageChunk)
    assert reply.content == "Hello, world"
    assert reply.additional_kwargs["reasoning_content"] == "let me think"
    assert reply.usage_metadata == USAGE
    assert reply.response_metadata["cost"] == 0.001
    usage = next(e for e in seen if e.type is EventType.MODEL_USAGE).data
    assert usage["cache_read_tokens"] == 64 and usage["cost_usd"] == 0.001
    assert next(e for e in seen if e.type is EventType.MODEL_REASONING).data == {
        "text": "let me think"
    }
    assert model.streamed == 1


async def test_tiny_chunks_are_coalesced() -> None:
    bus = Recorder()
    pieces = ["ab"] * 200
    model = FakeStreamingModel([_text(*pieces)])

    await _runtime(model, bus).run(session_id="s", user_id="u", message="hi")

    deltas = _deltas(bus)
    assert "".join(e.data["text"] for e in deltas) == "ab" * 200
    assert len(deltas) <= 400 // 64 + 2  # the first piece at once, then >= 64 chars each
    assert all(len(e.data["text"]) >= 64 for e in deltas[1:-1])


async def test_slow_chunks_flush_on_time() -> None:
    bus = Recorder()
    model = FakeStreamingModel([_text("a", "b", "c")], delay=0.06)

    await _runtime(model, bus).run(session_id="s", user_id="u", message="hi")

    assert [e.data["text"] for e in _deltas(bus)] == ["a", "b", "c"]


async def test_streamed_tool_calls_aggregate_and_run() -> None:
    bus = Recorder()
    call_chunks = [
        AIMessageChunk(
            content="",
            tool_call_chunks=[{"name": "echo", "args": '{"x": ', "id": "c1", "index": 0}],
        ),
        AIMessageChunk(
            content="",
            tool_call_chunks=[{"name": None, "args": "1}", "id": None, "index": 0}],
        ),
    ]
    model = FakeStreamingModel([call_chunks, _text("done")])

    reply = await _runtime(model, bus).run(session_id="s", user_id="u", message="hi")

    assert reply.content == "done"
    completed = [e for e in bus.delivered if e.type is EventType.TOOL_COMPLETED]
    assert completed and completed[0].data["ok"] is True
    started = next(e for e in bus.delivered if e.type is EventType.TOOL_STARTED)
    assert started.data["args"] == {"x": 1}
    assert [e.data["step"] for e in _deltas(bus)] == [1]  # the second model call's text


async def test_deltas_skip_the_event_log_and_the_audit_sink(tmp_path: Path) -> None:
    audited: list[AgentEvent] = []

    class Sink:
        def write(self, event: AgentEvent) -> None:
            audited.append(event)

    log = SqliteEventLogStore(SqliteDatabase(tmp_path / "index.db"))
    bus = EventStoreBus(log, audit=Sink())
    model = FakeStreamingModel([_text("Hello", " there")])
    seen: list[AgentEvent] = []
    listener = await _subscribe(bus, "r1", seen)

    await _runtime(model, bus).run(session_id="s", user_id="u", message="hi", run_id="r1")
    await listener

    assert any(e.type is EventType.ASSISTANT_DELTA for e in seen)  # live subscribers get them
    logged = await log.read(run_id="r1")
    assert EventType.ASSISTANT_DELTA not in {e.type for e in logged}
    assert EventType.ASSISTANT_DELTA not in {e.type for e in audited}
    assert [e.sequence for e in logged] == list(range(len(logged)))
    assert next(e for e in logged if e.type is EventType.ASSISTANT_MESSAGE).data["content"] == (
        "Hello there"
    )


async def test_a_cancel_stops_the_stream_promptly() -> None:
    bus = Recorder()
    model = FakeStreamingModel([_text(*["x"] * 1000)], delay=0.01)
    token = CancellationToken()
    runtime = _runtime(model, bus)
    task = asyncio.create_task(runtime.run(session_id="s", user_id="u", message="hi", cancel=token))
    while not _deltas(bus):
        await asyncio.sleep(0.005)
    token.cancel()
    started = time.monotonic()
    with pytest.raises(RunCancelled):
        await task
    assert time.monotonic() - started < 0.5


async def test_a_hard_cancel_interrupts_the_stream() -> None:
    bus = Recorder()
    model = FakeStreamingModel([_text("first", "never")], delay=0.05)
    task = asyncio.create_task(_runtime(model, bus).run(session_id="s", user_id="u", message="hi"))
    while not _deltas(bus):
        await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "never" not in "".join(e.data["text"] for e in _deltas(bus))


async def test_the_deadline_interrupts_a_stream() -> None:
    bus = Recorder()
    model = FakeStreamingModel([_text(*["x"] * 100)], delay=0.05)
    runtime = _runtime(model, bus, limits=AgentLimits(max_runtime_seconds=0.2))
    with pytest.raises(RuntimeDeadlineExceeded):
        await runtime.run(session_id="s", user_id="u", message="hi")


async def test_a_model_that_cannot_stream_falls_back_once_and_is_remembered() -> None:
    bus = Recorder()

    class NoStream(FakeChatModel):
        attempts = 0

        def astream(self, messages: Any, **kwargs: Any) -> Any:
            type(self).attempts += 1
            raise NotImplementedError

    model = NoStream([ai("one"), ai("two")])
    runtime = _runtime(model, bus)

    assert (await runtime.run(session_id="s", user_id="u", message="a")).content == "one"
    assert (await runtime.run(session_id="s", user_id="u", message="b")).content == "two"
    assert NoStream.attempts == 1
    assert _deltas(bus) == []


async def test_a_duck_typed_model_without_astream_still_works() -> None:
    bus = Recorder()
    reply = await _runtime(FakeChatModel([ai("plain")]), bus).run(
        session_id="s", user_id="u", message="a"
    )
    assert reply.content == "plain"
    assert [e.type for e in bus.delivered] == [
        EventType.RUN_STARTED,
        EventType.ASSISTANT_MESSAGE,
        EventType.RUN_COMPLETED,
    ]
