"""Unit tests for the event model and the in-process event bus.

Driven by ``InMemoryEventBus`` and ``asyncio`` only — no model, no stores.
"""

from __future__ import annotations

import asyncio

from agent_stack.core.event_bus import InMemoryEventBus
from agent_stack.core.events import AgentEvent, EventType


def _event(run_id: str, event_type: EventType = EventType.RUN_STARTED) -> AgentEvent:
    # ``sequence=99`` is a deliberate placeholder; the bus must replace it.
    return AgentEvent.create(event_type, run_id=run_id, session_id="s1", sequence=99)


async def _drain(bus: InMemoryEventBus, run_id: str) -> list[AgentEvent]:
    return [event async for event in bus.subscribe(run_id)]


async def test_sequence_is_monotonic_and_starts_at_zero() -> None:
    bus = InMemoryEventBus()
    task = asyncio.create_task(_drain(bus, "run-1"))
    await asyncio.sleep(0)  # let the subscriber register its queue

    for _ in range(3):
        await bus.publish(_event("run-1"))
    await bus.publish(_event("run-1", EventType.RUN_COMPLETED))

    events = await asyncio.wait_for(task, timeout=1)

    assert [event.sequence for event in events] == [0, 1, 2, 3]
    assert events[-1].type is EventType.RUN_COMPLETED


async def test_two_subscribers_receive_events_in_order() -> None:
    bus = InMemoryEventBus()
    first = asyncio.create_task(_drain(bus, "run-1"))
    second = asyncio.create_task(_drain(bus, "run-1"))
    await asyncio.sleep(0)

    await bus.publish(_event("run-1"))
    await bus.publish(_event("run-1", EventType.RUN_COMPLETED))

    results = await asyncio.wait_for(asyncio.gather(first, second), timeout=1)

    assert [[event.sequence for event in events] for events in results] == [[0, 1], [0, 1]]


async def test_subscription_terminates_after_terminal_event() -> None:
    bus = InMemoryEventBus()
    task = asyncio.create_task(_drain(bus, "run-1"))
    await asyncio.sleep(0)

    await bus.publish(_event("run-1", EventType.RUN_COMPLETED))

    events = await asyncio.wait_for(task, timeout=1)

    assert [event.type for event in events] == [EventType.RUN_COMPLETED]


async def test_publish_without_subscribers_does_not_raise() -> None:
    bus = InMemoryEventBus()

    await bus.publish(_event("nobody-listening"))
    await bus.publish(_event("nobody-listening", EventType.RUN_COMPLETED))


async def test_run_ids_are_isolated() -> None:
    bus = InMemoryEventBus()
    first = asyncio.create_task(_drain(bus, "run-a"))
    second = asyncio.create_task(_drain(bus, "run-b"))
    await asyncio.sleep(0)

    await bus.publish(_event("run-a"))
    await bus.publish(_event("run-b"))
    await bus.publish(_event("run-a", EventType.RUN_COMPLETED))
    await bus.publish(_event("run-b", EventType.RUN_COMPLETED))

    a, b = await asyncio.wait_for(asyncio.gather(first, second), timeout=1)

    assert [event.run_id for event in a] == ["run-a", "run-a"]
    assert [event.run_id for event in b] == ["run-b", "run-b"]
    assert [event.sequence for event in a] == [0, 1]
    assert [event.sequence for event in b] == [0, 1]
