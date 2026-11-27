"""The architectural invariants (plan section 92), executable.

Each test names the rule it guards. They drive the real runtime / run manager / stores with a
scripted fake model, or scan the source tree, so a refactor that breaks a rule fails here.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage

from agent_stack.core.agent import Agent
from agent_stack.core.approval import ApprovalBroker, ToolPolicy
from agent_stack.core.event_bus import InMemoryEventBus
from agent_stack.core.events import AgentEvent, EventType
from agent_stack.core.limits import AgentLimits
from agent_stack.core.locks import SessionLockManager
from agent_stack.core.run import RunManager, RunRequest, RunStatus
from agent_stack.core.runtime import AgentRuntime
from agent_stack.core.session import InMemorySessionStore
from agent_stack.persistence.conversation import FileConversationStore
from agent_stack.persistence.memory import InMemoryRunStore
from agent_stack.persistence.sqlite import SqliteCheckpointStore, SqliteDatabase
from agent_stack.tools.executor import ToolExecutor
from agent_stack.tools.registry import ToolRegistry
from tests.fakes import FailingTool, FakeChatModel, SleepTool, ai, call

SRC = Path(__file__).resolve().parents[2] / "src" / "agent_stack"
_TERMINAL_EVENTS = {EventType.RUN_COMPLETED, EventType.RUN_FAILED}
_TERMINAL_STATUSES = {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}


class CountingTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = "counts"
        self.runs = 0

    async def ainvoke(self, args: dict[str, Any]) -> str:
        self.runs += 1
        return f"{self.name} ok"


def _runtime(
    model: Any,
    tools: Sequence[Any] = (),
    *,
    store: Any = None,
    bus: InMemoryEventBus | None = None,
    limits: AgentLimits | None = None,
    policy: ToolPolicy | None = None,
    approvals: ApprovalBroker | None = None,
    locks: SessionLockManager | None = None,
) -> AgentRuntime:
    agent = Agent(
        name="assistant",
        model="test:model",
        tools=tuple(tools),
        limits=limits or AgentLimits(),
        tool_policy=policy or ToolPolicy(),
    )
    return AgentRuntime(
        agent=agent,
        model=model,
        tool_executor=ToolExecutor(ToolRegistry(list(tools))),
        session_store=store or InMemorySessionStore(),
        event_bus=bus,
        session_lock=locks,
        approvals=approvals,
    )


# 1 ── every executed tool call has exactly one matching ToolMessage ───────────
async def test_inv1_every_tool_call_gets_exactly_one_tool_message() -> None:
    store, broker = InMemorySessionStore(), ApprovalBroker()
    tools = [CountingTool("ok"), FailingTool(name="boom"), CountingTool("gated")]
    model = FakeChatModel(
        [
            ai(
                tool_calls=[
                    call("ok", id="a"),
                    call("boom", id="b"),  # raises
                    call("missing", id="c"),  # unknown tool
                    call("gated", id="d"),  # rejected by the human
                ]
            ),
            ai("done"),
        ]
    )
    runtime = _runtime(
        model,
        tools,
        store=store,
        approvals=broker,
        policy=ToolPolicy(requires_approval=frozenset({"gated"})),
    )

    async def reject_everything() -> None:
        while True:
            for request in broker.pending("r1"):
                broker.decide(request.id, run_id="r1", session_id="s", principal="u", approve=False)
            await asyncio.sleep(0.005)

    rejecter = asyncio.create_task(reject_everything())
    try:
        await asyncio.wait_for(
            runtime.run(session_id="s", user_id="u", message="go", run_id="r1"), 3.0
        )
    finally:
        rejecter.cancel()

    messages = await store.get_messages("s")
    asked = [c["id"] for m in messages if isinstance(m, AIMessage) for c in m.tool_calls]
    answered = [m.tool_call_id for m in messages if isinstance(m, ToolMessage)]
    assert sorted(asked) == ["a", "b", "c", "d"]
    assert Counter(answered) == Counter(asked)  # one answer per call, none extra


# 2 ── a session has at most one active run ────────────────────────────────────
class _ConcurrencyProbeModel:
    """Counts how many runs are inside ``ainvoke`` at once."""

    def __init__(self) -> None:
        self.active = 0
        self.peak = 0

    def bind_tools(self, tools: Any) -> _ConcurrencyProbeModel:
        return self

    async def ainvoke(self, messages: Any, **kwargs: Any) -> AIMessage:
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(0.05)
        self.active -= 1
        return ai("ok")


async def test_inv2_runs_in_one_session_never_overlap_but_other_sessions_do() -> None:
    model = _ConcurrencyProbeModel()
    runtime = _runtime(model, locks=SessionLockManager())

    await asyncio.gather(
        runtime.run(session_id="same", user_id="u", message="1"),
        runtime.run(session_id="same", user_id="u", message="2"),
    )
    assert model.peak == 1  # serialized

    model.peak = 0
    await asyncio.gather(
        runtime.run(session_id="a", user_id="u", message="1"),
        runtime.run(session_id="b", user_id="u", message="2"),
    )
    assert model.peak == 2  # the lock is per session, not global


# 3 ── a completed run is immutable ────────────────────────────────────────────
async def test_inv3_a_completed_run_cannot_be_changed() -> None:
    store, run_store, bus = InMemorySessionStore(), InMemoryRunStore(), InMemoryEventBus()
    runtime = _runtime(FakeChatModel([ai("done")]), store=store, bus=bus)
    manager = RunManager(runtime=runtime, run_store=run_store, event_bus=bus)

    handle = await manager.start(RunRequest(session_id="s", user_id="u", message="go"))
    await handle.result()
    before = (await run_store.get(handle.run_id), await store.get_messages("s"))

    await manager.cancel(handle.run_id)  # too late: must be a no-op
    accepted = await manager.send_input(handle.run_id, "more?")  # nobody is listening

    after = (await run_store.get(handle.run_id), await store.get_messages("s"))
    assert before[0] is not None and before[0].status is RunStatus.COMPLETED
    assert after == before
    assert accepted is False


# 4 ── messages are append-only ────────────────────────────────────────────────
async def test_inv4_messages_are_append_only(tmp_path: Path) -> None:
    conversation = FileConversationStore(tmp_path / "agent")
    runtime = _runtime(FakeChatModel([ai("first"), ai("second")]), store=conversation)

    await runtime.run(session_id="s", user_id="u", message="one")
    after_first = [(type(m).__name__, m.content) for m in await conversation.get_messages("s")]
    await runtime.run(session_id="s", user_id="u", message="two")
    after_second = [(type(m).__name__, m.content) for m in await conversation.get_messages("s")]

    assert after_second[: len(after_first)] == after_first  # earlier history untouched
    assert len(after_second) > len(after_first)


def test_inv4_stores_expose_no_way_to_edit_or_remove_a_message() -> None:
    allowed_message_api = {"get_messages", "append_messages"}
    for store in (InMemorySessionStore, FileConversationStore):
        names = {n for n in dir(store) if "message" in n.lower() and not n.startswith("_")}
        extra = names - allowed_message_api - {"read_messages", "list_messages"}
        assert not extra, f"{store.__name__} exposes {extra}: messages must be append-only"


# 5 ── mutating tool executions have an idempotency identity ───────────────────
async def test_inv5_a_replayed_run_does_not_execute_a_completed_call_again(
    tmp_path: Path,
) -> None:
    checkpoints = SqliteCheckpointStore(SqliteDatabase(tmp_path / "agent.db"))
    tool, store = CountingTool("mutate"), InMemorySessionStore()
    script = [ai(tool_calls=[call("mutate", id="call-1")]), ai("done")]

    first = _runtime(FakeChatModel(list(script)), [tool], store=store)
    await first.run(
        session_id="s", user_id="u", message="go", run_id="r1", checkpoint_store=checkpoints
    )
    replay = _runtime(FakeChatModel(list(script)), [tool], store=store)
    await replay.run(
        session_id="s", user_id="u", message="go", run_id="r1", checkpoint_store=checkpoints
    )

    assert tool.runs == 1  # (run_id, tool_call_id) is the idempotency key
    assert await checkpoints.completed_tool_call_ids("r1") == {"call-1"}


# 6 ── every run terminates via a known path ───────────────────────────────────
class _CrashingModel:
    def bind_tools(self, tools: Any) -> _CrashingModel:
        return self

    async def ainvoke(self, messages: Any, **kwargs: Any) -> AIMessage:
        raise RuntimeError("provider exploded")


async def _terminal_outcome(
    model: Any,
    tools: Sequence[Any] = (),
    limits: AgentLimits | None = None,
    cancel_after: float | None = None,
) -> tuple[RunStatus, list[AgentEvent]]:
    bus, run_store = InMemoryEventBus(), InMemoryRunStore()
    manager = RunManager(
        runtime=_runtime(model, tools, bus=bus, limits=limits), run_store=run_store, event_bus=bus
    )
    handle = await manager.start(RunRequest(session_id="s", user_id="u", message="go"))
    events: list[AgentEvent] = []

    async def collect() -> None:
        async for event in handle.events():
            events.append(event)

    collector = asyncio.create_task(collect())
    if cancel_after is not None:
        await asyncio.sleep(cancel_after)
        await manager.cancel(handle.run_id)
    # The run may legitimately end in an exception (that is what several scenarios test).
    with contextlib.suppress(BaseException):
        await asyncio.wait_for(handle.result(), 3.0)
    await asyncio.wait_for(collector, 3.0)
    run = await run_store.get(handle.run_id)
    assert run is not None
    return run.status, events


async def test_inv6_every_run_ends_in_a_terminal_status_with_at_most_one_terminal_event() -> None:
    scenarios = {
        "success": (FakeChatModel([ai("ok")]), (), None, None),
        "model error": (_CrashingModel(), (), None, None),
        "deadline": (
            FakeChatModel([ai(tool_calls=[call("slow", id="s")])]),
            [SleepTool("slow", 5.0)],
            AgentLimits(max_runtime_seconds=0.1, tool_timeout_seconds=30.0),
            None,
        ),
        "cancelled": (
            FakeChatModel([ai(tool_calls=[call("slow", id="s")])]),
            [SleepTool("slow", 5.0)],
            None,
            0.1,
        ),
    }
    for name, (model, tools, limits, cancel_after) in scenarios.items():
        status, events = await _terminal_outcome(model, tools, limits, cancel_after)

        assert status in _TERMINAL_STATUSES, f"{name}: ended as {status}"
        terminals = [e for e in events if e.type in _TERMINAL_EVENTS]
        assert len(terminals) <= 1, f"{name}: {len(terminals)} terminal events"
        if name != "cancelled":  # a hard cancel publishes no terminal event by design
            assert len(terminals) == 1, f"{name}: no terminal event"
    assert (await _terminal_outcome(FakeChatModel([ai("ok")])))[0] is RunStatus.COMPLETED


# 7 ── gateways never execute tools directly ───────────────────────────────────
def _imports_and_calls(path: Path) -> tuple[set[str], set[str]]:
    tree = ast.parse(path.read_text())
    modules: set[str] = set()
    calls: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
            modules.update(f"{node.module}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            modules.update(a.name for a in node.names)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            calls.add(node.func.attr)
    return modules, calls


def test_inv7_gateways_never_touch_tools_or_the_executor() -> None:
    offenders = []
    for path in (SRC / "gateways").rglob("*.py"):
        modules, calls = _imports_and_calls(path)
        if any(m.startswith("agent_stack.tools") for m in modules):
            offenders.append(f"{path.name} imports tools")
        if {"ainvoke", "invoke", "execute_one", "execute_many"} & calls:
            offenders.append(f"{path.name} calls a tool/executor")
    assert offenders == []


# 8 ── core never depends on a UI/transport framework or on the layers above ───
_FRAMEWORKS = {"textual", "fastapi", "starlette", "uvicorn", "telegram", "aiogram"}


def test_inv8_core_and_infrastructure_import_no_ui_framework() -> None:
    offenders = []
    for path in SRC.rglob("*.py"):
        if "gateways" in path.relative_to(SRC).parts:
            continue
        modules, _ = _imports_and_calls(path)
        bad = {m for m in modules if m.split(".")[0] in _FRAMEWORKS}
        if bad:
            offenders.append(f"{path.relative_to(SRC)}: {sorted(bad)}")
    assert offenders == []


def test_inv8_core_does_not_import_the_application_or_gateway_layers() -> None:
    offenders = []
    for path in (SRC / "core").rglob("*.py"):
        modules, _ = _imports_and_calls(path)
        bad = {m for m in modules if m.startswith(("agent_stack.gateways", "agent_stack.app"))}
        if bad:
            offenders.append(f"{path.name}: {sorted(bad)}")
    assert offenders == []


# 9 ── the environment is read in a few deliberate places only ─────────────────
#: Where reading the process environment is part of the job: configuration, the composition
#: root and entry point, the subprocess allow-list, and secret detection. A new read elsewhere
#: should be a decision, not an accident.
_ENV_READERS = {
    "app/config.py",
    "app/bootstrap.py",
    "observability/cli.py",
    "observability/redact.py",
    "tools/builtins/terminal.py",
}


def test_inv9_the_environment_is_read_only_where_it_is_meant_to_be() -> None:
    offenders = []
    for path in SRC.rglob("*.py"):
        relative = path.relative_to(SRC).as_posix()
        if relative in _ENV_READERS:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            reads_environ = isinstance(node, ast.Attribute) and (
                isinstance(node.value, ast.Name)
                and node.value.id == "os"
                and node.attr in {"environ", "getenv"}
            )
            loads_dotenv = isinstance(node, ast.Name) and node.id == "load_dotenv"
            if reads_environ or loads_dotenv:
                offenders.append(relative)
                break
    assert offenders == []
