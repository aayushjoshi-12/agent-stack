"""P29 durable event log + replay, P30 conversation branching (real SQLite + files)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from agent_stack.app.application import Application
from agent_stack.app.config import AppConfig
from agent_stack.core.agent import Agent
from agent_stack.core.approval import ApprovalBroker
from agent_stack.core.errors import PersistenceError
from agent_stack.core.events import AgentEvent, EventType
from agent_stack.core.factory import AgentFactory
from agent_stack.core.run import RunManager
from agent_stack.core.runtime import AgentRuntime
from agent_stack.gateways.base import MessageEnvelope
from agent_stack.observability.metrics import Metrics
from agent_stack.persistence.conversation import FileConversationStore
from agent_stack.persistence.event_log import EventStoreBus, SqliteEventLogStore
from agent_stack.persistence.memory import InMemoryRunStore
from agent_stack.persistence.sqlite import SqliteDatabase, SqliteSessionStore
from agent_stack.tools.executor import ToolExecutor
from agent_stack.tools.registry import ToolRegistry
from tests.fakes import FakeChatModel, ai, call

pytestmark = pytest.mark.integration


class EchoTool:
    name = "echo"
    description = "Echo."

    async def ainvoke(self, args: dict[str, Any]) -> str:
        return f"echo: {args}"


@dataclass
class World:
    app: Application
    db: SqliteDatabase
    root: Path


def _world(tmp_path: Path, model: FakeChatModel) -> World:
    db = SqliteDatabase(tmp_path / "index.db")
    log = SqliteEventLogStore(db)
    store = SqliteSessionStore(db, FileConversationStore(tmp_path), log)
    bus = EventStoreBus(log)
    tools = [EchoTool()]
    agent = Agent(
        name="assistant",
        model="test:model",
        tools=tuple(tools),  # type: ignore[arg-type]
        system_prompt="t",
    )
    runtime = AgentRuntime(
        agent=agent,
        model=model,
        tool_executor=ToolExecutor(ToolRegistry(tools)),  # type: ignore[arg-type]
        session_store=store,
        event_bus=bus,
    )
    app = Application(
        config=AppConfig(),
        agent=agent,
        factory=AgentFactory(session_store=store, event_bus=bus),
        session_store=store,
        run_manager=RunManager(runtime=runtime, run_store=InMemoryRunStore(), event_bus=bus),
        event_bus=bus,
        approvals=ApprovalBroker(),
        metrics=Metrics(),
        event_log=log,
    )
    return World(app, db, tmp_path)


async def _say(app: Application, session: str, text: str) -> tuple[str, list[AgentEvent]]:
    handle = await app.send_message(MessageEnvelope(principal="u1", session=session, text=text))
    await handle.result()
    return handle.run_id, [e async for e in handle.events()]


async def test_every_event_type_is_logged_and_replay_matches_live(tmp_path: Path) -> None:
    w = _world(tmp_path, FakeChatModel([ai(tool_calls=[call("echo", {"x": 1})]), ai("done")]))
    run_id, live = await _say(w.app, "s1", "go")

    replayed = await w.app.replay(run_id=run_id)

    assert {e.type for e in live} >= {
        EventType.RUN_STARTED,
        EventType.TOOL_STARTED,
        EventType.TOOL_COMPLETED,
        EventType.ASSISTANT_MESSAGE,
        EventType.RUN_COMPLETED,
    }
    assert [(e.id, e.type, e.sequence) for e in replayed] == [
        (e.id, e.type, e.sequence) for e in live
    ]
    assert [e.sequence for e in replayed] == list(range(len(live)))
    assert [e.timestamp for e in replayed] == [e.timestamp for e in live]
    cut = await w.app.replay(run_id=run_id, up_to_sequence=1)
    assert [e.sequence for e in cut] == [0, 1]


async def test_events_survive_restart(tmp_path: Path) -> None:
    w = _world(tmp_path, FakeChatModel([ai("done")]))
    run_id, live = await _say(w.app, "s1", "hi")
    w.db.close()

    reopened = SqliteEventLogStore(SqliteDatabase(tmp_path / "index.db"))
    events = await reopened.read(session_id="s1")

    assert [e.id for e in events] == [e.id for e in live]
    assert {e.run_id for e in events} == {run_id}


async def test_persisted_events_are_redacted(tmp_path: Path) -> None:
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
    w = _world(
        tmp_path,
        FakeChatModel(
            [ai(tool_calls=[call("echo", {"api_key": "hunter2", "q": secret})]), ai("x")]
        ),
    )
    run_id, _ = await _say(w.app, "s1", "go")

    rows = await w.db.query("SELECT data_json FROM event_log WHERE run_id = ?", (run_id,))
    blob = " ".join(str(r["data_json"]) for r in rows)
    assert "hunter2" not in blob
    assert secret not in blob


async def test_branch_forks_at_event_and_preserves_prefix(tmp_path: Path) -> None:
    w = _world(tmp_path, FakeChatModel([ai("a1"), ai("a2")]))
    _, events1 = await _say(w.app, "s1", "first")
    await _say(w.app, "s1", "second")
    done = next(e for e in events1 if e.type == EventType.RUN_COMPLETED)

    branch_id = await w.app.branch_session("s1", done.id)

    branch = await w.app.get_session(branch_id)
    assert branch is not None
    assert branch.branch_of == "s1"
    assert branch.branch_point_event_id == done.id
    texts = [(i.kind, i.text) for i in await w.app.session_history(branch_id)]
    assert texts == [("user", "first"), ("assistant", "a1")]
    parent_texts = [(i.kind, i.text) for i in await w.app.session_history("s1")]
    assert parent_texts[-2:] == [("user", "second"), ("assistant", "a2")]


async def test_branch_and_parent_diverge_after_fork(tmp_path: Path) -> None:
    w = _world(tmp_path, FakeChatModel([ai("a1"), ai("branch-reply"), ai("parent-reply")]))
    _, events1 = await _say(w.app, "s1", "first")
    done = next(e for e in events1 if e.type == EventType.RUN_COMPLETED)
    branch_id = await w.app.branch_session("s1", done.id)

    await _say(w.app, branch_id, "what if B")
    await _say(w.app, "s1", "what if A")

    branch_rows = [(i.kind, i.text) for i in await w.app.session_history(branch_id)]
    parent_rows = [(i.kind, i.text) for i in await w.app.session_history("s1")]
    assert branch_rows[2:] == [("user", "what if B"), ("assistant", "branch-reply")]
    assert parent_rows[2:] == [("user", "what if A"), ("assistant", "parent-reply")]


async def test_branch_writes_do_not_touch_parent(tmp_path: Path) -> None:
    w = _world(tmp_path, FakeChatModel([ai("a1"), ai("b1")]))
    _, events1 = await _say(w.app, "s1", "first")
    done = next(e for e in events1 if e.type == EventType.RUN_COMPLETED)
    parent_file = tmp_path / "sessions" / "s1" / "transcript.jsonl"
    before = parent_file.read_bytes()

    branch_id = await w.app.branch_session("s1", done.id)
    await _say(w.app, branch_id, "diverge")

    assert parent_file.read_bytes() == before
    parent = await w.app.get_session("s1")
    assert parent is not None and parent.version == 1


async def test_branch_mid_run_event_excludes_that_runs_turn(tmp_path: Path) -> None:
    w = _world(tmp_path, FakeChatModel([ai("a1")]))
    _, events1 = await _say(w.app, "s1", "first")
    started = next(e for e in events1 if e.type == EventType.RUN_STARTED)

    branch_id = await w.app.branch_session("s1", started.id)

    assert await w.app.session_history(branch_id) == []


async def test_branch_publishes_event_and_rejects_foreign_event(tmp_path: Path) -> None:
    w = _world(tmp_path, FakeChatModel([ai("a1"), ai("b1")]))
    _, e1 = await _say(w.app, "s1", "first")
    _, e2 = await _say(w.app, "s2", "other")
    done = next(e for e in e1 if e.type == EventType.RUN_COMPLETED)

    branch_id = await w.app.branch_session("s1", done.id)

    logged = await w.app.replay(session_id="s1")
    forks = [e for e in logged if e.type == EventType.SESSION_BRANCHED]
    assert [dict(f.data) for f in forks] == [
        {
            "parent_session_id": "s1",
            "branch_point_event_id": done.id,
            "branch_session_id": branch_id,
        }
    ]
    with pytest.raises(PersistenceError):
        await w.app.branch_session("s1", e2[0].id)
    with pytest.raises(PersistenceError):
        await w.app.branch_session("nope", done.id)


async def test_branch_fields_persist_across_restart(tmp_path: Path) -> None:
    w = _world(tmp_path, FakeChatModel([ai("a1")]))
    _, e1 = await _say(w.app, "s1", "first")
    branch_id = await w.app.branch_session("s1", e1[-1].id)
    w.db.close()

    db = SqliteDatabase(tmp_path / "index.db")
    store = SqliteSessionStore(db, FileConversationStore(tmp_path))
    branch = await store.get(branch_id)
    assert branch is not None and branch.branch_of == "s1"
