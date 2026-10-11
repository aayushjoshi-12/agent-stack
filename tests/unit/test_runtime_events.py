"""Runtime → event emission tests.

The runtime generates its own ``run_id``, so these tests use a *recording* bus: a real
``InMemoryEventBus`` subclass that also remembers every published event. That keeps the
real sequencing behaviour while letting a test inspect the internally-generated run id.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from agent_stack.core.agent import Agent
from agent_stack.core.errors import StepLimitExceeded
from agent_stack.core.event_bus import InMemoryEventBus
from agent_stack.core.events import AgentEvent, EventType
from agent_stack.core.limits import AgentLimits
from agent_stack.core.runtime import AgentRuntime
from agent_stack.core.session import InMemorySessionStore
from agent_stack.tools.executor import ToolExecutor
from agent_stack.tools.registry import ToolRegistry
from tests.fakes import FakeChatModel, ai, call


class RecordingEventBus(InMemoryEventBus):
    """An ``InMemoryEventBus`` that also records every published event."""

    def __init__(self) -> None:
        super().__init__()
        self.published: list[AgentEvent] = []

    async def publish(self, event: AgentEvent) -> None:
        self.published.append(event)
        await super().publish(event)


class EchoTool:
    """A tiny duck-typed tool; the registry/executor only need name/description/ainvoke."""

    name = "echo"
    description = "Echo the arguments back."

    async def ainvoke(self, args: dict[str, Any]) -> str:
        return f"echo: {args}"


def _build_runtime(
    model: Any,
    tools: Sequence[Any],
    store: InMemorySessionStore,
    event_bus: InMemoryEventBus | None,
    *,
    limits: AgentLimits | None = None,
) -> AgentRuntime:
    registry = ToolRegistry(tools)  # type: ignore[arg-type]
    executor = ToolExecutor(registry)
    agent = Agent(
        name="assistant",
        model="test:model",
        tools=tuple(tools),  # type: ignore[arg-type]
        system_prompt="You are a test assistant.",
        limits=limits or AgentLimits(),
    )
    return AgentRuntime(
        agent=agent,
        model=model,
        tool_executor=executor,
        session_store=store,
        event_bus=event_bus,
    )


async def test_tool_round_trip_emits_ordered_events() -> None:
    store = InMemorySessionStore()
    bus = RecordingEventBus()
    model = FakeChatModel([ai(tool_calls=[call("echo", {"x": 1})]), ai("done")])
    runtime = _build_runtime(model, [EchoTool()], store, bus)

    await runtime.run(session_id="s1", user_id="u1", message="go")

    assert [event.type for event in bus.published] == [
        EventType.RUN_STARTED,
        EventType.ASSISTANT_MESSAGE,
        EventType.TOOL_STARTED,
        EventType.TOOL_COMPLETED,
        EventType.ASSISTANT_MESSAGE,
        EventType.RUN_COMPLETED,
    ]


async def test_events_share_run_id_and_carry_session_id() -> None:
    store = InMemorySessionStore()
    bus = RecordingEventBus()
    model = FakeChatModel([ai(tool_calls=[call("echo", {"x": 1})]), ai("done")])
    runtime = _build_runtime(model, [EchoTool()], store, bus)

    await runtime.run(session_id="s1", user_id="u1", message="go")

    run_ids = {event.run_id for event in bus.published}
    assert len(run_ids) == 1
    assert "" not in run_ids
    assert all(event.session_id == "s1" for event in bus.published)


async def test_event_payloads_describe_the_run() -> None:
    store = InMemorySessionStore()
    bus = RecordingEventBus()
    model = FakeChatModel([ai(tool_calls=[call("echo", {"x": 1})]), ai("done")])
    runtime = _build_runtime(model, [EchoTool()], store, bus)

    await runtime.run(session_id="s1", user_id="u1", message="go")

    started = bus.published[0]
    assert started.data == {"agent": "assistant", "user_id": "u1"}

    assistant = bus.published[1]
    assert assistant.data == {"content": "", "tool_calls": ["echo"]}

    tool_started = next(event for event in bus.published if event.type is EventType.TOOL_STARTED)
    assert tool_started.data == {"tool": "echo", "tool_call_id": "call-1"}

    completed = bus.published[-1]
    assert completed.data == {"steps": 2, "tool_calls": 1}


async def test_model_reasoning_is_emitted_before_the_answer() -> None:
    store = InMemorySessionStore()
    bus = RecordingEventBus()
    model = FakeChatModel([ai("the answer", reasoning="let me think")])
    runtime = _build_runtime(model, [], store, bus)

    await runtime.run(session_id="s1", user_id="u1", message="go")

    assert [event.type for event in bus.published] == [
        EventType.RUN_STARTED,
        EventType.MODEL_REASONING,
        EventType.ASSISTANT_MESSAGE,
        EventType.RUN_COMPLETED,
    ]
    reasoning = bus.published[1]
    assert reasoning.data == {"text": "let me think"}


async def test_no_reasoning_event_when_the_model_thinks_nothing() -> None:
    store = InMemorySessionStore()
    bus = RecordingEventBus()
    model = FakeChatModel([ai("the answer")])
    runtime = _build_runtime(model, [], store, bus)

    await runtime.run(session_id="s1", user_id="u1", message="go")

    assert EventType.MODEL_REASONING not in {event.type for event in bus.published}


async def test_run_failed_is_emitted_and_reraised() -> None:
    store = InMemorySessionStore()
    bus = RecordingEventBus()
    model = FakeChatModel([ai(tool_calls=[call("echo", {})]), ai(tool_calls=[call("echo", {})])])
    runtime = _build_runtime(model, [EchoTool()], store, bus, limits=AgentLimits(max_steps=1))

    with pytest.raises(StepLimitExceeded):
        await runtime.run(session_id="s1", user_id="u1", message="go")

    assert bus.published[-1].type is EventType.RUN_FAILED
    assert "error" in bus.published[-1].data


async def test_runs_without_event_bus() -> None:
    store = InMemorySessionStore()
    runtime = _build_runtime(FakeChatModel([ai("hi")]), [], store, None)

    response = await runtime.run(session_id="s1", user_id="u1", message="hello")

    assert response.content == "hi"
