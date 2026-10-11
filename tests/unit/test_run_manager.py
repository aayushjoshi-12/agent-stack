"""Unit tests for the run manager and run handle.

Driven entirely by deterministic doubles: ``FakeChatModel`` for the model, the in-memory
session/run stores, and a real ``InMemoryEventBus`` where event ordering matters.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from agent_stack.core.agent import Agent
from agent_stack.core.event_bus import InMemoryEventBus
from agent_stack.core.events import EventType
from agent_stack.core.limits import AgentLimits
from agent_stack.core.run import Run, RunManager, RunRequest, RunStatus
from agent_stack.core.runtime import AgentRuntime
from agent_stack.core.session import InMemorySessionStore, Session
from agent_stack.persistence.memory import InMemoryRunStore
from agent_stack.tools.executor import ToolExecutor
from agent_stack.tools.registry import ToolRegistry
from tests.fakes import FakeChatModel, ai, call


class EchoTool:
    """A tiny duck-typed tool; the registry/executor only need name/description/ainvoke."""

    name = "echo"
    description = "Echo the arguments back."

    async def ainvoke(self, args: dict[str, Any]) -> str:
        return f"echo: {args}"


class SleepTool:
    """A tool that sleeps; lets a run stay suspended until cancellation lands."""

    name = "sleep"
    description = "Sleep briefly."

    async def ainvoke(self, args: dict[str, Any]) -> str:
        await asyncio.sleep(0.2)
        return "slept"


class RaisingModel:
    """A model that always raises; keeps the failure at the model boundary."""

    async def ainvoke(self, messages: Any, **kwargs: Any) -> AIMessage:
        raise RuntimeError("model exploded")


class RecordingRunStore(InMemoryRunStore):
    """An in-memory run store that records every status transition."""

    def __init__(self) -> None:
        super().__init__()
        self.transitions: list[tuple[str, RunStatus]] = []

    async def save(self, run: Run) -> None:
        self.transitions.append((run.id, run.status))
        await super().save(run)


def _build_runtime(
    model: Any,
    tools: Sequence[Any],
    store: InMemorySessionStore,
    event_bus: InMemoryEventBus | None,
) -> AgentRuntime:
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
        event_bus=event_bus,
    )


def _request(message: str = "hello", user_id: str = "u1") -> RunRequest:
    return RunRequest(session_id="s1", user_id=user_id, message=message)


async def test_run_lifecycle_transitions() -> None:
    store = InMemorySessionStore()
    run_store = RecordingRunStore()
    runtime = _build_runtime(FakeChatModel([ai("done")]), [], store, None)
    manager = RunManager(runtime=runtime, run_store=run_store)

    handle = await manager.start(_request())

    result = await handle.result()
    assert result.content == "done"

    run = await run_store.get(handle.run_id)
    assert run is not None
    assert run.status == RunStatus.COMPLETED
    assert [status for _run_id, status in run_store.transitions] == [
        RunStatus.PENDING,
        RunStatus.RUNNING,
        RunStatus.COMPLETED,
    ]


async def test_completed_run_records_step_and_tool_counts() -> None:
    """A completed run's record carries the loop counters, not zeros."""
    store = InMemorySessionStore()
    run_store = InMemoryRunStore()
    runtime = _build_runtime(
        FakeChatModel([ai(tool_calls=[call("echo", {})]), ai("done")]),
        [EchoTool()],
        store,
        None,
    )
    manager = RunManager(runtime=runtime, run_store=run_store)

    handle = await manager.start(_request())

    result = await handle.result()
    assert result.content == "done"

    run = await run_store.get(handle.run_id)
    assert run is not None
    assert run.status == RunStatus.COMPLETED
    assert run.step_count == 2
    assert run.tool_call_count == 1


async def test_failed_run_recorded() -> None:
    store = InMemorySessionStore()
    run_store = InMemoryRunStore()
    runtime = _build_runtime(RaisingModel(), [], store, None)
    manager = RunManager(runtime=runtime, run_store=run_store)

    handle = await manager.start(_request())

    with pytest.raises(RuntimeError, match="model exploded"):
        await handle.result()

    run = await run_store.get(handle.run_id)
    assert run is not None
    assert run.status == RunStatus.FAILED
    assert run.error is not None
    assert "model exploded" in run.error


async def test_cancel_run() -> None:
    store = InMemorySessionStore()
    await store.save(Session(id="s1", owner_id="u1", agent_name="assistant"))
    run_store = InMemoryRunStore()
    # A model that always calls tools keeps the run alive until cancellation lands.
    looping = FakeChatModel([ai(tool_calls=[call("echo", {})]) for _ in range(10_000)])
    runtime = _build_runtime(looping, [EchoTool()], store, None)
    manager = RunManager(runtime=runtime, run_store=run_store)

    handle = await manager.start(_request())
    run_id = handle.run_id

    await manager.cancel(run_id)

    run = await run_store.get(run_id)
    assert run is not None
    assert run.status == RunStatus.CANCELLED
    # The store's session was left untouched by the interrupted run.
    session = await store.get("s1")
    assert session is not None
    assert session.version == 0
    assert await store.get_messages("s1") == []


async def test_cancel_run_mid_flight_persists_cancelled() -> None:
    """Cancellation that lands while the run is suspended is persisted by the drive task."""
    store = InMemorySessionStore()
    await store.save(Session(id="s1", owner_id="u1", agent_name="assistant"))
    run_store = InMemoryRunStore()
    runtime = _build_runtime(
        FakeChatModel([ai(tool_calls=[call("sleep")]), ai("done")]),
        [SleepTool()],
        store,
        None,
    )
    manager = RunManager(runtime=runtime, run_store=run_store)

    handle = await manager.start(_request())
    run_id = handle.run_id
    await asyncio.sleep(0.01)  # let the run reach its tool sleep

    await manager.cancel(run_id)

    run = await run_store.get(run_id)
    assert run is not None
    assert run.status == RunStatus.CANCELLED
    # The interrupted run never finished its message append/save.
    session = await store.get("s1")
    assert session is not None
    assert session.version == 0
    assert await store.get_messages("s1") == []
    with pytest.raises(asyncio.CancelledError):
        await handle.result()


async def test_cancel_of_unknown_run_is_a_noop() -> None:
    store = InMemorySessionStore()
    run_store = InMemoryRunStore()
    runtime = _build_runtime(FakeChatModel([ai("done")]), [], store, None)
    manager = RunManager(runtime=runtime, run_store=run_store)

    await manager.cancel("missing")  # must not raise


async def test_recover_finds_incomplete_runs_on_startup() -> None:
    store = InMemorySessionStore()
    run_store = InMemoryRunStore()
    runtime = _build_runtime(FakeChatModel([ai("done")]), [], store, None)
    manager = RunManager(runtime=runtime, run_store=run_store)

    # Simulate a crash: a run stuck in RUNNING from a previous process.
    await run_store.save(Run(id="stale-1", session_id="s1", status=RunStatus.RUNNING))
    await run_store.save(Run(id="stale-2", session_id="s1", status=RunStatus.PENDING))
    await run_store.save(Run(id="fresh", session_id="s1", status=RunStatus.COMPLETED))

    count = await manager.recover()

    assert count == 2
    stale = await run_store.get("stale-1")
    assert stale is not None
    assert stale.status == RunStatus.FAILED
    assert stale.error == "interrupted before startup recovery"
    untouched = await run_store.get("fresh")
    assert untouched is not None
    assert untouched.status == RunStatus.COMPLETED


async def test_handle_result_and_events() -> None:
    store = InMemorySessionStore()
    run_store = InMemoryRunStore()
    bus = InMemoryEventBus()
    runtime = _build_runtime(FakeChatModel([ai("hi"), ai("bye")]), [EchoTool()], store, bus)
    manager = RunManager(runtime=runtime, run_store=run_store, event_bus=bus)

    handle = await manager.start(_request())

    result = await handle.result()
    assert result.content == "hi"

    events = [event async for event in handle.events()]
    types = [event.type for event in events]
    assert types[0] is EventType.RUN_STARTED
    assert types[-1] is EventType.RUN_COMPLETED
    assert all(event.run_id == handle.run_id for event in events)
    assert all(event.session_id == "s1" for event in events)


async def test_handle_events_yield_bus_sequence_order() -> None:
    store = InMemorySessionStore()
    run_store = InMemoryRunStore()
    bus = InMemoryEventBus()
    runtime = _build_runtime(FakeChatModel([ai("done")]), [], store, bus)
    manager = RunManager(runtime=runtime, run_store=run_store, event_bus=bus)

    handle = await manager.start(_request())

    events = [event async for event in handle.events()]
    sequences = [event.sequence for event in events]
    assert sequences == sorted(sequences)
    assert [event.type for event in events] == [
        EventType.RUN_STARTED,
        EventType.ASSISTANT_MESSAGE,
        EventType.RUN_COMPLETED,
    ]


async def test_start_honours_parent_run_id() -> None:
    store = InMemorySessionStore()
    run_store = InMemoryRunStore()
    runtime = _build_runtime(FakeChatModel([ai("done")]), [], store, None)
    manager = RunManager(runtime=runtime, run_store=run_store)

    request = RunRequest(
        session_id="s1",
        user_id="u1",
        message="go",
        parent_run_id="parent-1",
    )
    handle = await manager.start(request)
    await handle.result()

    run = await run_store.get(handle.run_id)
    assert run is not None
    assert run.parent_run_id == "parent-1"


async def test_two_runs_in_parallel_both_complete() -> None:
    store = InMemorySessionStore()
    run_store = InMemoryRunStore()
    runtime = _build_runtime(FakeChatModel([ai("done"), ai("done")]), [], store, None)
    manager = RunManager(runtime=runtime, run_store=run_store)

    first = await manager.start(_request(user_id="u1"))
    second = await manager.start(_request(user_id="u2"))

    results = await asyncio.gather(first.result(), second.result())
    assert [result.content for result in results] == ["done", "done"]

    for handle in (first, second):
        run = await run_store.get(handle.run_id)
        assert run is not None
        assert run.status == RunStatus.COMPLETED
