"""Phase 20 — subagents: delegation, event linkage, and parent-side control.

Deterministic end-to-end tests built from the real ``RunManager`` + ``AgentRuntime`` +
``InMemoryEventBus`` and the fake model, mirroring ``test_run_manager.py``. A fake agent
factory hands each child agent its own pre-built runtime so dependent code never touches a
real provider.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from langchain_core.messages import HumanMessage

from agent_stack.app.application import Application
from agent_stack.app.config import AppConfig
from agent_stack.core.agent import Agent, SubAgentSpec
from agent_stack.core.event_bus import InMemoryEventBus
from agent_stack.core.events import AgentEvent, EventType
from agent_stack.core.factory import AgentFactory
from agent_stack.core.limits import AgentLimits
from agent_stack.core.run import RunManager, RunRequest, RunStatus
from agent_stack.core.run_context import RunContext, run_context
from agent_stack.core.runtime import AgentRuntime
from agent_stack.core.session import InMemorySessionStore
from agent_stack.gateways.base import MessageEnvelope
from agent_stack.persistence.memory import InMemoryRunStore
from agent_stack.subagents.delegation import (
    delegate,
    subagent_feed,
    subagent_interrupt,
    subagent_status,
)
from agent_stack.tools.executor import ToolExecutor
from agent_stack.tools.registry import ToolRegistry
from tests.fakes import FakeChatModel, SleepTool, ai, call


class RecordingEventBus(InMemoryEventBus):
    """An ``InMemoryEventBus`` that remembers every published event."""

    def __init__(self) -> None:
        super().__init__()
        self.published: list[AgentEvent] = []

    async def publish(self, event: AgentEvent) -> None:
        self.published.append(event)
        await super().publish(event)


class FakeFactory:
    """A duck-typed ``AgentFactory``: maps an agent name to a ready runtime."""

    def __init__(self, runtimes: dict[str, AgentRuntime]) -> None:
        self._runtimes = runtimes

    def create(self, agent: Agent) -> AgentRuntime:
        return self._runtimes[agent.name]


class ConcurrencyProbe:
    """A tool that records how many invocations overlap in time."""

    name = "probe"
    description = "Record overlapping invocations."

    def __init__(self, seconds: float = 0.1) -> None:
        self.active = 0
        self.max_active = 0
        self._seconds = seconds

    async def ainvoke(self, args: dict[str, Any]) -> str:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(self._seconds)
        self.active -= 1
        return "probed"


def _runtime(
    agent: Agent,
    model: Any,
    tools: Sequence[Any],
    session_store: InMemorySessionStore,
    event_bus: InMemoryEventBus | None,
) -> AgentRuntime:
    registry = ToolRegistry(list(tools))  # type: ignore[arg-type]
    return AgentRuntime(
        agent=agent,
        model=model,
        tool_executor=ToolExecutor(registry),
        session_store=session_store,
        event_bus=event_bus,
    )


def _context(manager: RunManager, agent: Agent, *, run_id: str, depth: int = 0) -> RunContext:
    return RunContext(
        run_id=run_id,
        session_id="s1",
        user_id="u1",
        depth=depth,
        parent_run_id=None,
        agent=agent,
        services=manager,
    )


async def test_runs_subagent() -> None:
    session_store = InMemorySessionStore()
    run_store = InMemoryRunStore()
    bus = RecordingEventBus()

    explorer = Agent(name="explorer", model="test:model")
    child_model = FakeChatModel([ai("found: src/agent_stack/core/run.py")])
    child_runtime = _runtime(explorer, child_model, [], session_store, bus)

    parent_model = FakeChatModel(
        [
            ai(tool_calls=[call("delegate", {"agent_name": "explorer", "task": "find run.py"})]),
            ai("done"),
        ]
    )
    parent = Agent(
        name="assistant",
        model="test:model",
        tools=(delegate,),
        subagents=(SubAgentSpec("explorer", "Read-only exploration.", explorer),),
    )
    parent_runtime = _runtime(parent, parent_model, [delegate], session_store, bus)
    manager = RunManager(
        runtime=parent_runtime,
        run_store=run_store,
        event_bus=bus,
        agent_factory=FakeFactory({"explorer": child_runtime}),
    )

    handle = await manager.start(RunRequest(session_id="s1", user_id="u1", message="go"))
    result = await handle.result()

    assert result.content == "done"

    children = await run_store.list_runs(handle.run_id)
    assert len(children) == 1
    assert children[0].parent_run_id == handle.run_id
    assert children[0].status is RunStatus.COMPLETED

    # The compact summary (child text), not the transcript, reached the parent's context.
    assert any(
        "src/agent_stack/core/run.py" in str(message.content) for message in parent_model.calls[1]
    )


async def test_subagent_events_stream_to_parent_scope() -> None:
    session_store = InMemorySessionStore()
    run_store = InMemoryRunStore()
    bus = RecordingEventBus()

    explorer = Agent(name="explorer", model="test:model")
    child_runtime = _runtime(explorer, FakeChatModel([ai("child done")]), [], session_store, bus)

    parent_model = FakeChatModel(
        [
            ai(tool_calls=[call("delegate", {"agent_name": "explorer", "task": "look"})]),
            ai("done"),
        ]
    )
    parent = Agent(
        name="assistant",
        model="test:model",
        tools=(delegate,),
        subagents=(SubAgentSpec("explorer", "Read-only exploration.", explorer),),
    )
    parent_runtime = _runtime(parent, parent_model, [delegate], session_store, bus)
    manager = RunManager(
        runtime=parent_runtime,
        run_store=run_store,
        event_bus=bus,
        agent_factory=FakeFactory({"explorer": child_runtime}),
    )

    handle = await manager.start(RunRequest(session_id="s1", user_id="u1", message="go"))
    await handle.result()
    parent_stream = [event async for event in handle.events()]

    started = next(event for event in bus.published if event.type is EventType.SUBAGENT_STARTED)
    completed = next(event for event in bus.published if event.type is EventType.SUBAGENT_COMPLETED)
    child_run_id = str(started.data["child_run_id"])

    # Every event the child emitted is tagged with the parent's run id.
    child_events = [event for event in bus.published if event.run_id == child_run_id]
    assert child_events
    assert all(event.parent_run_id == handle.run_id for event in child_events)
    assert any(event.type is EventType.RUN_STARTED for event in child_events)
    assert any(event.type is EventType.RUN_COMPLETED for event in child_events)

    # The parent's own stream carries the matching lifecycle events on its scope.
    parent_types = [event.type for event in parent_stream]
    assert EventType.SUBAGENT_STARTED in parent_types
    assert EventType.SUBAGENT_COMPLETED in parent_types
    assert started.run_id == handle.run_id
    assert completed.run_id == handle.run_id
    # Parent-scope events carry the parent's session, matching their run_id.
    assert started.session_id == "s1"
    assert completed.session_id == "s1"
    assert completed.data["child_run_id"] == child_run_id
    assert completed.data["status"] == RunStatus.COMPLETED.value


async def test_subagent_status_reports_progress() -> None:
    session_store = InMemorySessionStore()
    run_store = InMemoryRunStore()
    bus = RecordingEventBus()

    sleep = SleepTool("sleep", 0.2)
    explorer = Agent(name="explorer", model="test:model", tools=(sleep,))
    child_model = FakeChatModel([ai(tool_calls=[call("sleep")]), ai("done")])
    child_runtime = _runtime(explorer, child_model, [sleep], session_store, bus)
    manager = RunManager(runtime=child_runtime, run_store=run_store, event_bus=bus)

    child = await manager.start(
        RunRequest(
            session_id="child-s",
            user_id="u1",
            message="go",
            parent_run_id="parent-1",
            depth=1,
        )
    )
    await asyncio.sleep(0.05)  # the child has emitted one assistant step and is in its tool

    with run_context(_context(manager, explorer, run_id="parent-1")):
        output = await subagent_status.ainvoke({"run_id": child.run_id})

    assert "running" in output
    assert "steps=1" in output

    await child.result()


async def test_subagent_interrupt_cancels_child() -> None:
    session_store = InMemorySessionStore()
    run_store = InMemoryRunStore()
    bus = RecordingEventBus()

    sleep = SleepTool("sleep", 0.2)
    explorer = Agent(name="explorer", model="test:model", tools=(sleep,))
    looping = FakeChatModel([ai(tool_calls=[call("sleep")]) for _ in range(10_000)])
    child_runtime = _runtime(explorer, looping, [sleep], session_store, bus)
    manager = RunManager(runtime=child_runtime, run_store=run_store, event_bus=bus)

    child = await manager.start(
        RunRequest(
            session_id="child-s",
            user_id="u1",
            message="go",
            parent_run_id="parent-1",
            depth=1,
        )
    )
    await asyncio.sleep(0.05)

    with run_context(_context(manager, explorer, run_id="parent-1")):
        output = await subagent_interrupt.ainvoke({"run_id": child.run_id})

    assert "Interrupted" in output
    run = await run_store.get(child.run_id)
    assert run is not None
    assert run.status is RunStatus.CANCELLED

    # The parent sees the child's outcome as a subagent.completed event on its scope.
    completed = next(event for event in bus.published if event.type is EventType.SUBAGENT_COMPLETED)
    assert completed.run_id == "parent-1"
    assert completed.data["status"] == RunStatus.CANCELLED.value


async def test_subagent_feed_injects_message_mid_run() -> None:
    session_store = InMemorySessionStore()
    run_store = InMemoryRunStore()
    bus = RecordingEventBus()

    sleep = SleepTool("sleep", 0.2)
    explorer = Agent(name="explorer", model="test:model", tools=(sleep,))
    child_model = FakeChatModel([ai(tool_calls=[call("sleep")]), ai("done")])
    child_runtime = _runtime(explorer, child_model, [sleep], session_store, bus)
    manager = RunManager(runtime=child_runtime, run_store=run_store, event_bus=bus)

    child = await manager.start(
        RunRequest(
            session_id="child-s",
            user_id="u1",
            message="go",
            parent_run_id="parent-1",
            depth=1,
        )
    )
    await asyncio.sleep(0.05)  # the child is inside its tool call

    with run_context(_context(manager, explorer, run_id="parent-1")):
        output = await subagent_feed.ainvoke({"run_id": child.run_id, "message": "focus on run.py"})

    assert "Sent input" in output
    await child.result()

    # The injected message was appended before the child's second model call.
    humans = [
        message.content for message in child_model.calls[1] if isinstance(message, HumanMessage)
    ]
    assert "focus on run.py" in humans


async def test_enforces_subagent_depth() -> None:
    session_store = InMemorySessionStore()
    run_store = InMemoryRunStore()
    explorer = Agent(name="explorer", model="test:model")
    parent = Agent(
        name="assistant",
        model="test:model",
        subagents=(SubAgentSpec("explorer", "Read-only exploration.", explorer),),
        limits=AgentLimits(max_subagent_depth=2),
    )
    manager = RunManager(
        runtime=_runtime(parent, FakeChatModel([ai("done")]), [], session_store, None),
        run_store=run_store,
    )

    with run_context(_context(manager, parent, run_id="parent-1", depth=2)):
        output = await delegate.ainvoke({"agent_name": "explorer", "task": "dive deeper"})

    assert "depth limit" in output
    assert await manager.list_child_ids("parent-1") == []


async def test_parallel_subagents_run_concurrently() -> None:
    session_store = InMemorySessionStore()
    run_store = InMemoryRunStore()
    bus = RecordingEventBus()

    probe = ConcurrencyProbe()
    explorer = Agent(name="explorer", model="test:model", tools=(probe,))
    researcher = Agent(name="researcher", model="test:model", tools=(probe,))
    children = {
        "explorer": _runtime(
            explorer,
            FakeChatModel([ai(tool_calls=[call("probe")]), ai("explorer done")]),
            [probe],
            session_store,
            bus,
        ),
        "researcher": _runtime(
            researcher,
            FakeChatModel([ai(tool_calls=[call("probe")]), ai("researcher done")]),
            [probe],
            session_store,
            bus,
        ),
    }

    parent_model = FakeChatModel(
        [
            ai(
                tool_calls=[
                    call("delegate", {"agent_name": "explorer", "task": "a"}, id="c1"),
                    call("delegate", {"agent_name": "researcher", "task": "b"}, id="c2"),
                ]
            ),
            ai("done"),
        ]
    )
    parent = Agent(
        name="assistant",
        model="test:model",
        tools=(delegate,),
        subagents=(
            SubAgentSpec("explorer", "Read-only exploration.", explorer),
            SubAgentSpec("researcher", "Research.", researcher),
        ),
    )
    parent_runtime = _runtime(parent, parent_model, [delegate], session_store, bus)
    manager = RunManager(
        runtime=parent_runtime,
        run_store=run_store,
        event_bus=bus,
        agent_factory=FakeFactory(children),
    )

    handle = await manager.start(RunRequest(session_id="s1", user_id="u1", message="go"))
    await handle.result()

    assert probe.max_active == 2
    assert len(await run_store.list_runs(handle.run_id)) == 2


async def test_application_send_input() -> None:
    session_store = InMemorySessionStore()
    run_store = InMemoryRunStore()
    bus = RecordingEventBus()

    sleep = SleepTool("sleep", 0.2)
    agent = Agent(name="assistant", model="test:model", tools=(sleep,))
    model = FakeChatModel([ai(tool_calls=[call("sleep")]), ai("done")])
    runtime = _runtime(agent, model, [sleep], session_store, bus)
    manager = RunManager(runtime=runtime, run_store=run_store, event_bus=bus)
    application = Application(
        config=AppConfig(),
        agent=agent,
        factory=AgentFactory(session_store=session_store, event_bus=bus),
        session_store=session_store,
        run_manager=manager,
        event_bus=bus,
    )

    handle = await application.send_message(
        MessageEnvelope(principal="u1", session="s1", text="go")
    )
    await asyncio.sleep(0.05)

    assert await application.send_input(handle.run_id, "hurry up") is True
    assert await application.send_input("missing-run", "ignored") is False

    await handle.result()
    humans = [message.content for message in model.calls[1] if isinstance(message, HumanMessage)]
    assert "hurry up" in humans
