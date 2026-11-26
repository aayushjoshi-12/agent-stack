"""P22: todos survive a checkpoint and resume (they belong to the session, not a run)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agent_stack.core.agent import Agent
from agent_stack.core.run_context import RunContext, run_context
from agent_stack.core.session import Session
from agent_stack.core.state import TodoStatus
from agent_stack.persistence.conversation import FileConversationStore
from agent_stack.persistence.sqlite import SqliteDatabase, SqliteSessionStore
from agent_stack.persistence.todos import SqliteTodoStore
from agent_stack.tools.builtins.todos import todo_tools


@pytest.fixture
def db(tmp_path: Path) -> SqliteDatabase:
    return SqliteDatabase(tmp_path / "agent.db")


def _context(session: str, run: str = "r1") -> RunContext:
    return RunContext(
        run_id=run,
        session_id=session,
        user_id="u1",
        depth=0,
        parent_run_id=None,
        agent=Agent(name="assistant", model="test:model"),
        services=None,  # type: ignore[arg-type]
    )


def _tools(store: SqliteTodoStore) -> dict[str, Any]:
    return {t.name: t for t in todo_tools(store)}


# ── the store ────────────────────────────────────────────────────────────────
async def test_add_list_complete_in_order(db: SqliteDatabase) -> None:
    store = SqliteTodoStore(db)
    first = await store.add("s1", "write tests")
    second = await store.add("s1", "fix bug")

    assert (first.id, second.id) == (1, 2)
    assert await store.complete("s1", 1) is True
    items = await store.list_items("s1")
    assert [(i.id, i.text, i.status) for i in items] == [
        (1, "write tests", TodoStatus.DONE),
        (2, "fix bug", TodoStatus.PENDING),
    ]
    assert await store.complete("s1", 99) is False


async def test_todos_are_per_session(db: SqliteDatabase) -> None:
    store = SqliteTodoStore(db)
    await store.add("s1", "mine")
    await store.add("s2", "theirs")

    assert [i.text for i in await store.list_items("s1")] == ["mine"]
    assert [(i.id, i.text) for i in await store.list_items("s2")] == [(1, "theirs")]  # own ids
    assert await store.complete("s2", 1) is True
    assert (await store.list_items("s1"))[0].status is TodoStatus.PENDING


async def test_concurrent_adds_never_share_an_id(db: SqliteDatabase) -> None:
    store = SqliteTodoStore(db)

    added = await asyncio.gather(*(store.add("s1", f"task {n}") for n in range(20)))

    assert sorted(item.id for item in added) == list(range(1, 21))
    assert len(await store.list_items("s1")) == 20


async def test_todos_survive_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "agent.db"
    await SqliteTodoStore(SqliteDatabase(path)).add("s1", "finish the report")

    reopened = SqliteTodoStore(SqliteDatabase(path))

    assert [i.text for i in await reopened.list_items("s1")] == ["finish the report"]


async def test_deleting_a_session_removes_its_todos(tmp_path: Path) -> None:
    db = SqliteDatabase(tmp_path / "agent.db")
    sessions = SqliteSessionStore(db, FileConversationStore(tmp_path / "agent"))
    store = SqliteTodoStore(db)
    await sessions.save(Session(id="s1", owner_id="u", agent_name="a"))
    await sessions.save(Session(id="s2", owner_id="u", agent_name="a"))
    await store.add("s1", "gone with the session")
    await store.add("s2", "stays")

    await sessions.delete("s1")

    assert await store.list_items("s1") == []
    assert [i.text for i in await store.list_items("s2")] == ["stays"]


# ── the tools ────────────────────────────────────────────────────────────────
async def test_tools_are_list_add_complete(db: SqliteDatabase) -> None:
    assert set(_tools(SqliteTodoStore(db))) == {"todo_list", "todo_add", "todo_complete"}


async def test_todos_survive_a_resume_in_a_new_run_of_the_same_session(
    db: SqliteDatabase,
) -> None:
    tools = _tools(SqliteTodoStore(db))
    with run_context(_context("s1", run="run-1")):
        assert await tools["todo_list"].ainvoke({}) == "No todos."
        assert "Added todo 1" in await tools["todo_add"].ainvoke({"text": "step one"})
        await tools["todo_add"].ainvoke({"text": "step two"})
        assert "Completed todo 1" in await tools["todo_complete"].ainvoke({"id": 1})

    # The run died and was resumed (or the user sent another message): a different run id.
    with run_context(_context("s1", run="run-2")):
        assert await tools["todo_list"].ainvoke({}) == "[x] 1. step one\n[ ] 2. step two"
        assert await tools["todo_complete"].ainvoke({"id": 7}) == "No todo 7."


async def test_tools_do_not_see_another_sessions_todos(db: SqliteDatabase) -> None:
    tools = _tools(SqliteTodoStore(db))
    with run_context(_context("s1")):
        await tools["todo_add"].ainvoke({"text": "private"})
    with run_context(_context("s2")):
        assert await tools["todo_list"].ainvoke({}) == "No todos."


async def test_tools_refuse_outside_a_run(db: SqliteDatabase) -> None:
    tools = _tools(SqliteTodoStore(db))

    for name, args in (
        ("todo_list", {}),
        ("todo_add", {"text": "x"}),
        ("todo_complete", {"id": 1}),
    ):
        assert "only available inside a run" in await tools[name].ainvoke(args)


async def test_open_todos_are_shown_in_the_system_prompt_every_call(
    db: SqliteDatabase, tmp_path: Path
) -> None:
    from agent_stack.context.composer import SystemPromptComposer
    from agent_stack.context.instructions import InstructionsLoader
    from agent_stack.memory import MemoryIndex
    from agent_stack.skills import SkillLoader, SkillRegistry

    store = SqliteTodoStore(db)
    await store.add("s1", "write the parser")
    await store.add("s1", "run the tests")
    await store.complete("s1", 1)
    composer = SystemPromptComposer(
        instructions=InstructionsLoader(),
        skills=SkillRegistry(SkillLoader([tmp_path / "none"])),
        memory=MemoryIndex(tmp_path),
        workspace_root=tmp_path,
        todos=store,
    )

    with run_context(_context("s1")):
        parts = await composer.parts(base_prompt="BASE")
    assert parts[0] == "BASE"
    assert "[x] 1. write the parser" in parts[1]
    assert "[ ] 2. run the tests" in parts[1]
    assert await composer.parts(base_prompt="BASE") == ("BASE",)  # no run, no todo block
