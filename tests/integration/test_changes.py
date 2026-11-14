"""Files the agent created or edited: recorded through the real tools and run context."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from agent_stack.app.application import Application
from agent_stack.app.config import AppConfig
from agent_stack.core.agent import Agent
from agent_stack.core.approval import ApprovalBroker
from agent_stack.core.changes import make_diff
from agent_stack.core.events import AgentEvent, EventType
from agent_stack.core.factory import AgentFactory
from agent_stack.core.run import RunManager
from agent_stack.core.run_context import RunContext, run_context
from agent_stack.core.runtime import AgentRuntime
from agent_stack.core.workspace import WorkspacePolicy
from agent_stack.gateways.base import MessageEnvelope
from agent_stack.observability.metrics import Metrics
from agent_stack.persistence.changes import LocalChangeStore
from agent_stack.persistence.conversation import FileConversationStore
from agent_stack.persistence.event_log import EventStoreBus, SqliteEventLogStore
from agent_stack.persistence.memory import InMemoryRunStore
from agent_stack.persistence.sqlite import SqliteDatabase, SqliteSessionStore
from agent_stack.tools.builtins.filesystem import filesystem_tools
from agent_stack.tools.executor import ToolExecutor
from agent_stack.tools.registry import ToolRegistry
from tests.fakes import FakeChatModel, ai, call

pytestmark = pytest.mark.integration


@dataclass
class World:
    app: Application
    workspace: Path
    store: LocalChangeStore
    db: SqliteDatabase


def _world(tmp_path: Path, model: FakeChatModel) -> World:
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    db = SqliteDatabase(tmp_path / "index.db")
    log = SqliteEventLogStore(db)
    sessions = SqliteSessionStore(db, FileConversationStore(tmp_path / "conv"), log)
    bus = EventStoreBus(log)
    store = LocalChangeStore(db, tmp_path / "changes")
    tools = list(filesystem_tools(WorkspacePolicy(workspace), store))
    agent = Agent(name="assistant", model="test:model", tools=tuple(tools), system_prompt="t")  # type: ignore[arg-type]
    runtime = AgentRuntime(
        agent=agent,
        model=model,
        tool_executor=ToolExecutor(ToolRegistry(tools)),  # type: ignore[arg-type]
        session_store=sessions,
        event_bus=bus,
    )
    app = Application(
        config=AppConfig(workspace=workspace),
        agent=agent,
        factory=AgentFactory(session_store=sessions, event_bus=bus),
        session_store=sessions,
        run_manager=RunManager(runtime=runtime, run_store=InMemoryRunStore(), event_bus=bus),
        event_bus=bus,
        approvals=ApprovalBroker(),
        metrics=Metrics(),
        event_log=log,
        change_store=store,
    )
    return World(app, workspace, store, db)


async def _run(app: Application, session: str = "s1", text: str = "go") -> str:
    handle = await app.send_message(MessageEnvelope(principal="u1", session=session, text=text))
    await handle.result()
    return handle.run_id


def _write(path: str, content: str, cid: str = "w1") -> dict[str, Any]:
    return call("write_file", {"path": path, "content": content}, id=cid)


def _edit(path: str, old: str, new: str, cid: str = "e1") -> dict[str, Any]:
    return call("edit_file", {"path": path, "old": old, "new": new}, id=cid)


# ── the diff itself ──────────────────────────────────────────────────────────
def test_make_diff_counts_and_shapes() -> None:
    diff, added, removed, truncated = make_diff("a.py", "x = 1\ny = 2\n", "x = 1\ny = 3\nz = 4\n")

    assert (added, removed, truncated) == (2, 1, False)
    assert diff.splitlines()[:2] == ["--- a/a.py", "+++ b/a.py"]
    assert "-y = 2" in diff and "+y = 3" in diff and "+z = 4" in diff


def test_make_diff_of_a_new_file_is_all_additions_and_huge_diffs_are_cut() -> None:
    diff, added, removed, _ = make_diff("n.txt", None, "a\nb\n")
    assert diff.splitlines()[0] == "--- /dev/null" and (added, removed) == (2, 0)

    big, _, _, truncated = make_diff("big.txt", None, "x\n" * 10_000)
    assert truncated and len(big.splitlines()) == 4000


# ── recorded through the real tools ──────────────────────────────────────────
async def test_a_created_file_is_recorded_with_its_content_and_an_all_plus_diff(
    tmp_path: Path,
) -> None:
    w = _world(
        tmp_path, FakeChatModel([ai(tool_calls=[_write("notes/a.md", "# Hi\nline\n")]), ai("ok")])
    )

    await _run(w.app)

    (changed,) = await w.app.changed_files("s1")
    assert (changed.path, changed.kind, changed.edits) == ("notes/a.md", "created", 1)
    assert (changed.added, changed.removed) == (2, 0) and changed.before is None
    assert changed.after == "# Hi\nline\n" and "+# Hi" in changed.diff


async def test_an_edit_is_recorded_as_a_modification_with_the_real_diff(tmp_path: Path) -> None:
    w = _world(
        tmp_path, FakeChatModel([ai(tool_calls=[_edit("a.py", "y = 2", "y = 3")]), ai("ok")])
    )
    (w.workspace / "a.py").write_text("x = 1\ny = 2\n")

    await _run(w.app)

    (changed,) = await w.app.changed_files("s1")
    assert (changed.kind, changed.added, changed.removed) == ("modified", 1, 1)
    assert changed.before == "x = 1\ny = 2\n" and changed.after == "x = 1\ny = 3\n"
    assert "-y = 2" in changed.diff and "+y = 3" in changed.diff


async def test_several_edits_to_one_file_are_one_net_diff_and_a_created_file_stays_created(
    tmp_path: Path,
) -> None:
    model = FakeChatModel(
        [
            ai(tool_calls=[_write("f.txt", "one\ntwo\n", "c1")]),
            ai(tool_calls=[_edit("f.txt", "two", "three", "c2")]),
            ai(tool_calls=[_edit("f.txt", "one", "uno", "c3")]),
            ai("ok"),
        ]
    )
    w = _world(tmp_path, model)

    await _run(w.app)

    (changed,) = await w.app.changed_files("s1")
    assert (changed.kind, changed.edits) == ("created", 3)
    assert changed.before is None and changed.after == "uno\nthree\n"


async def test_an_edit_that_is_undone_leaves_an_empty_net_diff(tmp_path: Path) -> None:
    model = FakeChatModel(
        [
            ai(tool_calls=[_edit("a.txt", "keep", "temp", "c1")]),
            ai(tool_calls=[_edit("a.txt", "temp", "keep", "c2")]),
            ai("ok"),
        ]
    )
    w = _world(tmp_path, model)
    (w.workspace / "a.txt").write_text("keep\n")

    await _run(w.app)

    (changed,) = await w.app.changed_files("s1")
    assert (changed.edits, changed.added, changed.removed, changed.diff) == (2, 0, 0, "")


async def test_a_run_can_be_looked_at_on_its_own(tmp_path: Path) -> None:
    model = FakeChatModel(
        [
            ai(tool_calls=[_write("one.txt", "1\n", "c1")]),
            ai("first done"),
            ai(tool_calls=[_write("two.txt", "2\n", "c2")]),
            ai("second done"),
        ]
    )
    w = _world(tmp_path, model)
    first = await _run(w.app, text="first")
    second = await _run(w.app, text="second")

    assert [f.path for f in await w.app.changed_files("s1")] == ["one.txt", "two.txt"]
    assert [f.path for f in await w.app.changed_files("s1", first)] == ["one.txt"]
    assert [f.path for f in await w.app.changed_files("s1", second)] == ["two.txt"]
    assert await w.app.changed_files("another-session") == []


async def test_a_failed_edit_records_nothing(tmp_path: Path) -> None:
    w = _world(tmp_path, FakeChatModel([ai(tool_calls=[_edit("a.txt", "absent", "x")]), ai("ok")]))
    (w.workspace / "a.txt").write_text("hello\n")

    await _run(w.app)

    assert await w.app.changed_files("s1") == []


async def test_overwriting_a_binary_file_is_not_misreported_as_a_new_file(tmp_path: Path) -> None:
    w = _world(tmp_path, FakeChatModel([ai(tool_calls=[_write("logo.bin", "text now")]), ai("ok")]))
    (w.workspace / "logo.bin").write_bytes(b"\x89PNG\x00\x01binary")

    await _run(w.app)

    assert (w.workspace / "logo.bin").read_text() == "text now"  # the write itself still works
    assert await w.app.changed_files("s1") == []


async def test_a_recorder_that_fails_never_fails_the_tool(tmp_path: Path) -> None:
    class Broken:
        async def record(self, **_: Any) -> None:
            raise RuntimeError("disk full")

    workspace = tmp_path / "ws"
    workspace.mkdir()
    tools = {t.name: t for t in filesystem_tools(WorkspacePolicy(workspace), Broken())}
    context = RunContext(
        run_id="r1",
        session_id="s1",
        user_id="u",
        depth=0,
        parent_run_id=None,
        agent=Agent(name="a", model="m"),
        services=None,  # type: ignore[arg-type]
    )

    with run_context(context):
        result = await tools["write_file"].ainvoke({"path": "x.txt", "content": "hi"})

    assert result.startswith("Wrote") and (workspace / "x.txt").read_text() == "hi"


async def test_nothing_is_recorded_outside_a_run(tmp_path: Path) -> None:
    w = _world(tmp_path, FakeChatModel([]))
    tools = {t.name: t for t in filesystem_tools(WorkspacePolicy(w.workspace), w.store)}

    await tools["write_file"].ainvoke({"path": "x.txt", "content": "hi"})  # no run context

    assert await w.store.paths("s1") == []


async def test_a_subagents_edits_roll_up_into_the_parent_conversation(tmp_path: Path) -> None:
    w = _world(tmp_path, FakeChatModel([]))
    log = w.app.event_log
    assert log is not None
    await log.append(
        [
            AgentEvent.create(EventType.RUN_STARTED, run_id="parent", session_id="s1", sequence=0),
            AgentEvent.create(
                EventType.RUN_STARTED,
                run_id="child",
                session_id="child-session",
                sequence=0,
                parent_run_id="parent",
            ),
        ]
    )
    await w.store.record(
        session_id="child-session", run_id="child", path="x.py", before=None, after="v = 1\n"
    )

    (changed,) = await w.app.changed_files("s1")

    assert changed.path == "x.py" and changed.after == "v = 1\n"
    assert await w.app.changed_files("unrelated") == []


async def test_deleting_a_conversation_removes_its_change_snapshots(tmp_path: Path) -> None:
    w = _world(tmp_path, FakeChatModel([ai(tool_calls=[_write("a.txt", "x\n")]), ai("ok")]))
    await _run(w.app)
    assert list((tmp_path / "changes").rglob("*.after"))

    await w.app.delete_session("s1")

    assert await w.app.changed_files("s1") == []
    assert not list((tmp_path / "changes").rglob("*.after"))


async def test_the_index_holds_ids_and_paths_never_file_text(tmp_path: Path) -> None:
    w = _world(
        tmp_path,
        FakeChatModel([ai(tool_calls=[_write("secret.txt", "TOP-SECRET-BODY\n")]), ai("ok")]),
    )
    await _run(w.app)

    rows = await w.db.query("SELECT * FROM file_changes")

    assert rows and all("TOP-SECRET-BODY" not in str(tuple(r)) for r in rows)
