"""P21: persistent memory: facts persist across sessions, recall by keyword, namespaces isolate."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_stack.core.agent import Agent
from agent_stack.core.run_context import RunContext, run_context
from agent_stack.memory.store import SqliteMemoryStore
from agent_stack.persistence.sqlite import SqliteDatabase
from agent_stack.tools.builtins.memory import persistent_memory_tools


@pytest.fixture
def store(tmp_path: Path) -> SqliteMemoryStore:
    return SqliteMemoryStore(SqliteDatabase(tmp_path / "agent.db"))


# ── the store ────────────────────────────────────────────────────────────────
async def test_remember_then_get_and_update_keeps_created_at(store: SqliteMemoryStore) -> None:
    first = await store.remember("u1", "editor", "helix")
    second = await store.remember("u1", "editor", "neovim")

    assert (await store.get("u1", "editor")) == second
    assert second.value == "neovim"
    assert second.created_at == first.created_at  # an update is not a new fact
    assert second.updated_at >= first.updated_at


async def test_facts_persist_across_sessions_and_restarts(tmp_path: Path) -> None:
    path = tmp_path / "agent.db"
    await SqliteMemoryStore(SqliteDatabase(path)).remember("u1", "city", "Haridwar")

    reopened = SqliteMemoryStore(SqliteDatabase(path))  # a fresh process, same file

    item = await reopened.get("u1", "city")
    assert item is not None and item.value == "Haridwar"


async def test_recall_matches_every_word_in_key_or_value_case_insensitively(
    store: SqliteMemoryStore,
) -> None:
    await store.remember("u1", "preferred_language", "Python with type hints")
    await store.remember("u1", "editor", "Neovim")
    await store.remember("u1", "deploy_target", "AWS EC2 over ssh")

    assert [i.key for i in await store.recall("u1", "python")] == ["preferred_language"]
    assert [i.key for i in await store.recall("u1", "TYPE hints")] == ["preferred_language"]
    assert [i.key for i in await store.recall("u1", "ec2 ssh")] == ["deploy_target"]
    assert await store.recall("u1", "python ec2") == []  # words must all match one fact
    assert await store.recall("u1", "nothing like this") == []


async def test_recall_treats_wildcards_literally(store: SqliteMemoryStore) -> None:
    await store.remember("u1", "discount", "50% off")
    await store.remember("u1", "other", "unrelated")

    assert [i.key for i in await store.recall("u1", "%")] == ["discount"]
    assert [i.key for i in await store.recall("u1", "_")] == []  # '_' is not "any char"


async def test_empty_recall_lists_newest_first_and_respects_limit(
    store: SqliteMemoryStore,
) -> None:
    for key in ("a", "b", "c"):
        await store.remember("u1", key, key.upper())

    assert [i.key for i in await store.recall("u1", "")] == ["c", "b", "a"]
    assert [i.key for i in await store.list_items("u1", limit=2)] == ["c", "b"]


async def test_namespaces_isolate_data(store: SqliteMemoryStore) -> None:
    await store.remember("u1", "secret_project", "atlas")
    await store.remember("u2", "secret_project", "borealis")

    assert (await store.get("u1", "secret_project")).value == "atlas"  # type: ignore[union-attr]
    assert (await store.get("u2", "secret_project")).value == "borealis"  # type: ignore[union-attr]
    assert [i.value for i in await store.recall("u2", "project")] == ["borealis"]
    assert await store.forget("u1", "secret_project") is True
    assert await store.get("u2", "secret_project") is not None  # u2 untouched


async def test_forget_reports_whether_anything_was_removed(store: SqliteMemoryStore) -> None:
    await store.remember("u1", "k", "v")

    assert await store.forget("u1", "k") is True
    assert await store.forget("u1", "k") is False
    assert await store.get("u1", "k") is None


# ── the tools ────────────────────────────────────────────────────────────────
def _context(user: str, session: str) -> RunContext:
    agent = Agent(name="assistant", model="test:model")
    return RunContext(
        run_id="r",
        session_id=session,
        user_id=user,
        depth=0,
        parent_run_id=None,
        agent=agent,
        services=None,  # type: ignore[arg-type]
    )


def _tools(store: SqliteMemoryStore) -> dict[str, Any]:
    return {t.name: t for t in persistent_memory_tools(store)}


async def test_tools_are_remember_recall_forget(store: SqliteMemoryStore) -> None:
    assert set(_tools(store)) == {"remember", "recall", "forget"}


async def test_tools_round_trip_across_two_sessions_of_one_user(
    store: SqliteMemoryStore,
) -> None:
    tools = _tools(store)
    with run_context(_context("tui:me", "session-1")):
        assert "Remembered" in await tools["remember"].ainvoke(
            {"key": "tone", "value": "concise, no emojis"}
        )
    with run_context(_context("tui:me", "session-2")):  # a different session, same user
        assert await tools["recall"].ainvoke({"query": "tone"}) == "tone: concise, no emojis"
        assert "Forgot" in await tools["forget"].ainvoke({"key": "tone"})
        assert await tools["recall"].ainvoke({"query": ""}) == "Nothing remembered yet."


async def test_tools_never_leak_between_users(store: SqliteMemoryStore) -> None:
    tools = _tools(store)
    with run_context(_context("tui:alice", "s")):
        await tools["remember"].ainvoke({"key": "diet", "value": "vegan"})
    with run_context(_context("tui:bob", "s")):
        assert await tools["recall"].ainvoke({"query": "diet"}) == "Nothing remembered matches."
        assert "No remembered fact" in await tools["forget"].ainvoke({"key": "diet"})


async def test_tools_refuse_outside_a_run(store: SqliteMemoryStore) -> None:
    tools = _tools(store)

    for name, args in (
        ("remember", {"key": "k", "value": "v"}),
        ("recall", {}),
        ("forget", {"key": "k"}),
    ):
        assert "only available inside a run" in await tools[name].ainvoke(args)
    assert await store.list_items("user:tui:me") == []  # and nothing was written
