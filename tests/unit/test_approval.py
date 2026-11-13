"""P26: the approval flow, from the broker's binding rules to a run that pauses and resumes."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from langchain_core.messages import ToolMessage

from agent_stack.core.agent import Agent
from agent_stack.core.approval import ApprovalBroker, ApprovalStatus, ToolPolicy
from agent_stack.core.errors import ApprovalError
from agent_stack.core.event_bus import InMemoryEventBus
from agent_stack.core.events import AgentEvent, EventType
from agent_stack.core.runtime import AgentRuntime
from agent_stack.core.session import InMemorySessionStore
from agent_stack.tools.executor import ToolExecutor
from agent_stack.tools.registry import ToolRegistry
from tests.fakes import FakeChatModel, ai, call


class RecordingBus(InMemoryEventBus):
    def __init__(self) -> None:
        super().__init__()
        self.published: list[AgentEvent] = []

    async def publish(self, event: AgentEvent) -> None:
        self.published.append(event)
        await super().publish(event)


class CountingTool:
    """Records how many times it really ran, to prove a rejected call never executes."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.description = "A tool that counts its executions."
        self.runs = 0

    async def ainvoke(self, args: dict[str, Any]) -> str:
        self.runs += 1
        return f"{self.name} ran with {args}"


def _runtime(
    model: Any,
    tools: list[CountingTool],
    *,
    gated: frozenset[str],
    broker: ApprovalBroker,
    store: InMemorySessionStore,
    bus: InMemoryEventBus | None = None,
) -> AgentRuntime:
    agent = Agent(
        name="assistant",
        model="test:model",
        tools=tuple(tools),  # type: ignore[arg-type]
        tool_policy=ToolPolicy(requires_approval=gated),
    )
    return AgentRuntime(
        agent=agent,
        model=model,
        tool_executor=ToolExecutor(ToolRegistry(tools)),  # type: ignore[arg-type]
        session_store=store,
        event_bus=bus,
        approvals=broker,
    )


async def _until(predicate: Callable[[], Any], timeout: float = 2.0) -> None:
    """Poll until ``predicate`` is truthy; fail loudly instead of hanging the suite."""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


def _start(runtime: AgentRuntime, run_id: str, session: str = "s1") -> asyncio.Task[Any]:
    return asyncio.create_task(
        runtime.run(session_id=session, user_id="u1", message="go", run_id=run_id)
    )


def _tool_messages(store: InMemorySessionStore, session: str = "s1") -> Awaitable[list[Any]]:
    async def read() -> list[Any]:
        return [m for m in await store.get_messages(session) if isinstance(m, ToolMessage)]

    return read()


# ── the broker's rules ───────────────────────────────────────────────────────
def _request(broker: ApprovalBroker, run_id: str = "r1") -> Any:
    return broker.create(
        run_id=run_id,
        session_id="s1",
        principal="u1",
        tool="danger",
        tool_call_id="c1",
        args={"x": 1},
    )


async def test_decision_is_recorded_and_wakes_the_waiter() -> None:
    broker = ApprovalBroker()
    request = _request(broker)
    waiter = asyncio.create_task(broker.wait(request.id))
    await asyncio.sleep(0)

    broker.decide(request.id, run_id="r1", session_id="s1", principal="u1", approve=True)

    assert await waiter is ApprovalStatus.APPROVED
    assert broker.pending("r1") == []


async def test_approval_cannot_be_replayed_against_another_run() -> None:
    broker = ApprovalBroker()
    mine = _request(broker, "r1")
    _request(broker, "r2")

    for wrong in (
        {"run_id": "r2", "session_id": "s1", "principal": "u1"},  # another run
        {"run_id": "r1", "session_id": "other", "principal": "u1"},  # another session
        {"run_id": "r1", "session_id": "s1", "principal": "intruder"},  # another principal
    ):
        with pytest.raises(ApprovalError, match="does not belong"):
            broker.decide(mine.id, approve=True, **wrong)

    assert mine.status is ApprovalStatus.PENDING  # none of the attempts changed anything


async def test_a_decision_can_only_be_given_once() -> None:
    broker = ApprovalBroker()
    request = _request(broker)
    broker.decide(request.id, run_id="r1", session_id="s1", principal="u1", approve=False)

    with pytest.raises(ApprovalError, match="already rejected"):
        broker.decide(request.id, run_id="r1", session_id="s1", principal="u1", approve=True)
    with pytest.raises(ApprovalError, match="unknown approval"):
        broker.decide("nope", run_id="r1", session_id="s1", principal="u1", approve=True)


async def test_cancelling_a_waiting_run_marks_the_request_cancelled() -> None:
    broker = ApprovalBroker()
    request = _request(broker)
    waiter = asyncio.create_task(broker.wait(request.id))
    await asyncio.sleep(0)

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert request.status is ApprovalStatus.CANCELLED


# ── the runtime ──────────────────────────────────────────────────────────────
async def test_requires_approval_pauses_run() -> None:
    broker, store, tool = ApprovalBroker(), InMemorySessionStore(), CountingTool("danger")
    model = FakeChatModel([ai(tool_calls=[call("danger", {"x": 1})]), ai("done")])
    runtime = _runtime(model, [tool], gated=frozenset({"danger"}), broker=broker, store=store)

    task = _start(runtime, "r1")
    await _until(lambda: broker.pending("r1"))

    assert not task.done()  # paused, not finished
    assert tool.runs == 0  # and the tool has not run
    (request,) = broker.pending("r1")
    assert (request.tool, dict(request.args), request.principal) == ("danger", {"x": 1}, "u1")

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_approval_resumes_and_executes() -> None:
    broker, store, tool = ApprovalBroker(), InMemorySessionStore(), CountingTool("danger")
    model = FakeChatModel([ai(tool_calls=[call("danger", {"x": 1})]), ai("all done")])
    bus = RecordingBus()
    runtime = _runtime(
        model, [tool], gated=frozenset({"danger"}), broker=broker, store=store, bus=bus
    )

    task = _start(runtime, "r1")
    await _until(lambda: broker.pending("r1"))
    (request,) = broker.pending("r1")
    broker.decide(request.id, run_id="r1", session_id="s1", principal="u1", approve=True)
    result = await asyncio.wait_for(task, 2.0)

    assert result.content == "all done"
    assert tool.runs == 1
    (message,) = await _tool_messages(store)
    assert message.status != "error" and "danger ran" in str(message.content)
    kinds = [e.type for e in bus.published]
    assert kinds.index(EventType.APPROVAL_REQUIRED) < kinds.index(EventType.APPROVAL_RESOLVED)
    resolved = next(e for e in bus.published if e.type is EventType.APPROVAL_RESOLVED)
    assert resolved.data["status"] == "approved"


async def test_rejected_approval_returns_error_tool_message() -> None:
    broker, store, tool = ApprovalBroker(), InMemorySessionStore(), CountingTool("danger")
    model = FakeChatModel([ai(tool_calls=[call("danger", {"x": 1})]), ai("understood")])
    runtime = _runtime(model, [tool], gated=frozenset({"danger"}), broker=broker, store=store)

    task = _start(runtime, "r1")
    await _until(lambda: broker.pending("r1"))
    (request,) = broker.pending("r1")
    broker.decide(request.id, run_id="r1", session_id="s1", principal="u1", approve=False)
    result = await asyncio.wait_for(task, 2.0)

    assert result.content == "understood"  # the model saw the rejection and carried on
    assert tool.runs == 0  # the tool never ran
    (message,) = await _tool_messages(store)
    assert message.status == "error"
    assert "rejected" in str(message.content)


async def test_only_gated_calls_wait_and_order_is_preserved() -> None:
    broker, store = ApprovalBroker(), InMemorySessionStore()
    safe, danger = CountingTool("safe"), CountingTool("danger")
    model = FakeChatModel(
        [
            ai(tool_calls=[call("danger", {"n": 1}, id="c1"), call("safe", {"n": 2}, id="c2")]),
            ai("fin"),
        ]
    )
    runtime = _runtime(
        model, [safe, danger], gated=frozenset({"danger"}), broker=broker, store=store
    )

    task = _start(runtime, "r1")
    await _until(lambda: broker.pending("r1"))
    assert safe.runs == 0  # the whole batch waits for the human, in order
    (request,) = broker.pending("r1")
    broker.decide(request.id, run_id="r1", session_id="s1", principal="u1", approve=False)
    await asyncio.wait_for(task, 2.0)

    messages = await _tool_messages(store)
    assert [m.tool_call_id for m in messages] == ["c1", "c2"]  # model's call order kept
    assert [m.status for m in messages] == ["error", "success"]
    assert (danger.runs, safe.runs) == (0, 1)


async def test_an_ungated_tool_never_asks() -> None:
    broker, store, tool = ApprovalBroker(), InMemorySessionStore(), CountingTool("safe")
    model = FakeChatModel([ai(tool_calls=[call("safe", {})]), ai("ok")])
    runtime = _runtime(model, [tool], gated=frozenset({"danger"}), broker=broker, store=store)

    await asyncio.wait_for(_start(runtime, "r1"), 2.0)

    assert tool.runs == 1


async def test_cancelling_during_approval_stops_the_run() -> None:
    broker, store, tool = ApprovalBroker(), InMemorySessionStore(), CountingTool("danger")
    model = FakeChatModel([ai(tool_calls=[call("danger", {})]), ai("never")])
    runtime = _runtime(model, [tool], gated=frozenset({"danger"}), broker=broker, store=store)

    task = _start(runtime, "r1")
    await _until(lambda: broker.pending("r1"))
    (request,) = broker.pending("r1")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert request.status is ApprovalStatus.CANCELLED
    assert tool.runs == 0


async def test_human_time_does_not_count_against_the_run_deadline() -> None:
    from agent_stack.core.limits import AgentLimits

    broker, store, tool = ApprovalBroker(), InMemorySessionStore(), CountingTool("danger")
    model = FakeChatModel([ai(tool_calls=[call("danger", {})]), ai("finished")])
    agent = Agent(
        name="assistant",
        model="test:model",
        tools=(tool,),  # type: ignore[arg-type]
        tool_policy=ToolPolicy(requires_approval=frozenset({"danger"})),
        limits=AgentLimits(max_runtime_seconds=0.3),
    )
    runtime = AgentRuntime(
        agent=agent,
        model=model,
        tool_executor=ToolExecutor(ToolRegistry([tool])),  # type: ignore[arg-type]
        session_store=store,
        approvals=broker,
    )

    task = _start(runtime, "r1")
    await _until(lambda: broker.pending("r1"))
    await asyncio.sleep(0.5)  # the human is slower than the whole runtime budget
    (request,) = broker.pending("r1")
    broker.decide(request.id, run_id="r1", session_id="s1", principal="u1", approve=True)

    result = await asyncio.wait_for(task, 2.0)

    assert result.content == "finished"


async def test_run_status_mirrors_the_pause_and_resume() -> None:
    from agent_stack.core.run import RunManager, RunRequest, RunStatus
    from agent_stack.persistence.memory import InMemoryRunStore

    broker, store, tool = ApprovalBroker(), InMemorySessionStore(), CountingTool("danger")
    bus = InMemoryEventBus()
    model = FakeChatModel([ai(tool_calls=[call("danger", {})]), ai("done")])
    runtime = _runtime(
        model, [tool], gated=frozenset({"danger"}), broker=broker, store=store, bus=bus
    )
    manager = RunManager(runtime=runtime, run_store=InMemoryRunStore(), event_bus=bus)

    handle = await manager.start(RunRequest(session_id="s1", user_id="u1", message="go"))
    run_id = handle.run_id
    await _until(lambda: broker.pending(run_id))

    async def status() -> RunStatus:
        run = await manager.get_run(run_id)
        assert run is not None
        return run.status

    async with asyncio.timeout(2.0):
        while await status() is not RunStatus.WAITING_APPROVAL:
            await asyncio.sleep(0.005)

    (request,) = broker.pending(run_id)
    broker.decide(request.id, run_id=run_id, session_id="s1", principal="u1", approve=True)
    await asyncio.wait_for(handle.result(), 2.0)

    assert await status() is RunStatus.COMPLETED
