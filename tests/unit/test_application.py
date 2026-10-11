"""Unit tests for P18/P19: the Application service layer and the gateway contract.

Driven entirely by deterministic doubles (FakeChatModel, in-memory stores, a real
``InMemoryEventBus``) exactly like the run-manager tests; the only new thing on
the wire is the Application boundary and the MessageEnvelope. ``FakeGateway``
drives a full run using only the Application — its method bodies never reference
``agent_stack.core``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Any

from langchain_core.messages import AIMessage

from agent_stack.app.application import Application, IdentityResolver, SessionResolver
from agent_stack.app.config import AppConfig
from agent_stack.core.agent import Agent
from agent_stack.core.event_bus import InMemoryEventBus
from agent_stack.core.events import AgentEvent, EventType
from agent_stack.core.factory import AgentFactory
from agent_stack.core.limits import AgentLimits
from agent_stack.core.run import RunManager, RunStatus
from agent_stack.core.runtime import AgentRuntime
from agent_stack.core.session import InMemorySessionStore
from agent_stack.gateways.base import Gateway, MessageEnvelope
from agent_stack.persistence.memory import InMemoryRunStore
from agent_stack.tools.executor import ToolExecutor
from agent_stack.tools.registry import ToolRegistry
from tests.fakes import FakeChatModel, SleepTool, ai, call


class FakeGateway:
    """A gateway double proving the P18 boundary.

    Its method bodies only touch ``Application`` and ``MessageEnvelope`` — the
    same surface a real gateway gets. It never references ``agent_stack.core``.
    """

    def __init__(self) -> None:
        self.result: AIMessage | None = None

    async def handle_message(
        self,
        application: Application,
        envelope: MessageEnvelope,
    ) -> AsyncIterator[AgentEvent]:
        """Drive one message through the Application and stream its events."""
        handle = await application.send_message(envelope)
        async for event in handle.events():
            yield event
        self.result = await handle.result()


def _build_application(
    model: Any,
    tools: Sequence[Any] = (),
    *,
    event_bus: InMemoryEventBus | None = None,
) -> tuple[Application, InMemoryRunStore, InMemorySessionStore]:
    session_store = InMemorySessionStore()
    run_store = InMemoryRunStore()
    bus = event_bus or InMemoryEventBus()
    registry = ToolRegistry(list(tools))  # type: ignore[arg-type]
    executor = ToolExecutor(registry)
    agent = Agent(
        name="assistant",
        model="test:model",
        tools=tuple(tools),  # type: ignore[arg-type]
        system_prompt="You are a test assistant.",
        limits=AgentLimits(),
    )
    runtime = AgentRuntime(
        agent=agent,
        model=model,
        tool_executor=executor,
        session_store=session_store,
        event_bus=bus,
    )
    manager = RunManager(runtime=runtime, run_store=run_store, event_bus=bus)
    application = Application(
        config=AppConfig(),
        agent=agent,
        factory=AgentFactory(session_store=session_store, event_bus=bus),
        session_store=session_store,
        run_manager=manager,
        event_bus=bus,
    )
    return application, run_store, session_store


async def test_fake_gateway_drives_full_run_using_only_application() -> None:
    app, run_store, _session_store = _build_application(FakeChatModel([ai("done")]))
    gateway = FakeGateway()

    events = [
        event
        async for event in gateway.handle_message(
            app, MessageEnvelope(principal="u1", session="s1", text="hello")
        )
    ]

    # The double satisfies the P19 outbound contract.
    assert isinstance(gateway, Gateway)
    assert gateway.result is not None
    assert gateway.result.content == "done"

    types = [event.type for event in events]
    assert types[0] is EventType.RUN_STARTED
    assert types[-1] is EventType.RUN_COMPLETED
    assert all(event.session_id == "s1" for event in events)
    assert all(event.run_id == events[0].run_id for event in events)

    run = await run_store.get(events[0].run_id)
    assert run is not None
    assert run.status is RunStatus.COMPLETED

    # The session was created lazily by the run, not eagerly by the gateway.
    session = await app.get_session("s1")
    assert session is not None
    assert session.owner_id == "u1"
    assert session.agent_name == "assistant"


async def test_send_message_accepts_envelope_and_parent_run_id() -> None:
    app, run_store, _session_store = _build_application(FakeChatModel([ai("done")]))

    handle = await app.send_message(
        MessageEnvelope(principal="u1", session="s1", text="hello"),
        parent_run_id="parent-1",
    )
    await handle.result()

    run = await run_store.get(handle.run_id)
    assert run is not None
    assert run.status is RunStatus.COMPLETED
    assert run.parent_run_id == "parent-1"


async def test_cancel_run_returns_running_run_to_terminal_state() -> None:
    app, run_store, _session_store = _build_application(
        FakeChatModel([ai(tool_calls=[call("sleep")]) for _ in range(10_000)]),
        tools=[SleepTool("sleep", 0.2)],
    )

    handle = await app.send_message(MessageEnvelope(principal="u1", session="s1", text="loop"))
    await app.cancel_run(handle.run_id)

    run = await run_store.get(handle.run_id)
    assert run is not None
    assert run.status is RunStatus.CANCELLED


async def test_get_session_round_trips_a_created_session() -> None:
    app, _run_store, _session_store = _build_application(FakeChatModel([ai("done")]))
    assert await app.get_session("s1") is None

    handle = await app.send_message(MessageEnvelope(principal="u1", session="s1", text="hi"))
    await handle.result()  # the runtime creates the session lazily during the run

    session = await app.get_session("s1")
    assert session is not None
    assert session.owner_id == "u1"


async def test_subscribe_streams_live_events_for_a_running_run() -> None:
    app, _run_store, _session_store = _build_application(
        FakeChatModel([ai(tool_calls=[call("sleep")]), ai("done")]),
        tools=[SleepTool("sleep", 0.5)],
    )
    handle = await app.send_message(MessageEnvelope(principal="u1", session="s1", text="go"))
    await asyncio.sleep(0.01)  # let the run reach its tool sleep

    events = [event async for event in app.subscribe(handle.run_id)]

    # The subscription observes the run's tail (the run is still mid-tool), in
    # sequence order, ending with the terminal event.
    assert events, "the live subscription must observe the running run"
    assert events[-1].type is EventType.RUN_COMPLETED
    assert all(event.run_id == handle.run_id for event in events)
    assert [event.sequence for event in events] == sorted(event.sequence for event in events)


def test_identity_resolver_maps_gateway_identity_to_principal() -> None:
    resolver = IdentityResolver()
    assert resolver.resolve("telegram", "user-123") == "telegram:user-123"
    assert resolver.resolve("tui", "local-user") == "tui:local-user"


async def test_session_resolver_creates_and_reuses_default_session() -> None:
    store = InMemorySessionStore()
    resolver = SessionResolver(store, agent_name="assistant")

    session_id = await resolver.resolve("telegram:user-123")

    session = await store.get(session_id)
    assert session is not None
    assert session.owner_id == "telegram:user-123"
    assert session.agent_name == "assistant"
    assert session.status == "active"

    # Idempotent: the same principal resolves to the same session.
    assert await resolver.resolve("telegram:user-123") == session_id
