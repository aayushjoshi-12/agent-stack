"""Integration tests for P12: session locking and optimistic versioning.

These exercise *composition*, not just protocol conformance: the runtime's
``expected_version`` save flow against every real store, the conflict that results when a
concurrent writer wins the race, the per-session lock that serializes concurrent runs, and
the bootstrap wiring. Store-level CAS behaviour itself is covered exhaustively by
``tests/contract/test_session_store_contract.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from agent_stack.app.application import Application
from agent_stack.app.bootstrap import build_application
from agent_stack.app.config import AppConfig
from agent_stack.core.agent import Agent
from agent_stack.core.errors import SessionVersionConflictError
from agent_stack.core.limits import AgentLimits
from agent_stack.core.locks import SessionLockManager
from agent_stack.core.run import RunManager, RunRequest
from agent_stack.core.runtime import AgentRuntime
from agent_stack.core.session import InMemorySessionStore, Session
from agent_stack.persistence.conversation import FileConversationStore
from agent_stack.persistence.memory import InMemoryRunStore
from agent_stack.persistence.sqlite import SqliteDatabase, SqliteSessionStore
from agent_stack.tools.executor import ToolExecutor
from agent_stack.tools.registry import ToolRegistry
from tests.fakes import FakeChatModel, ai, call


class SleepTool:
    """A tool that sleeps; gives a second run a window to interleave."""

    name = "sleep"
    description = "Sleep briefly."

    async def ainvoke(self, args: dict[str, Any]) -> str:
        await asyncio.sleep(0.2)
        return "slept"


def _memory(tmp_path: Path) -> Any:
    return InMemorySessionStore()


def _file(tmp_path: Path) -> Any:
    return FileConversationStore(tmp_path / "agent")


def _sqlite(tmp_path: Path) -> Any:
    return SqliteSessionStore(
        SqliteDatabase(tmp_path / "index.db"),
        FileConversationStore(tmp_path / "agent"),
    )


STORE_FACTORIES = [_memory, _file, _sqlite]
STORE_IDS = ["in-memory", "file", "sqlite"]


def _runtime(
    session_store: Any,
    model: Any,
    *,
    tools: Sequence[Any] = (),
    lock: SessionLockManager | None = None,
) -> AgentRuntime:
    registry = ToolRegistry(list(tools))
    executor = ToolExecutor(registry)
    agent = Agent(
        name="assistant",
        model="test:model",
        tools=tuple(tools),
        system_prompt="You are a test assistant.",
        limits=AgentLimits(),
    )
    return AgentRuntime(
        agent=agent,
        model=model,
        tool_executor=executor,
        session_store=session_store,
        session_lock=lock,
    )


def _manager(
    session_store: Any,
    model: Any,
    *,
    tools: Sequence[Any] = (),
    lock: SessionLockManager | None = None,
) -> RunManager:
    return RunManager(
        runtime=_runtime(session_store, model, tools=tools, lock=lock),
        run_store=InMemoryRunStore(),
    )


@pytest.mark.integration
@pytest.mark.parametrize("factory", STORE_FACTORIES, ids=STORE_IDS)
async def test_lost_update_detected_by_version_mismatch(factory: Any, tmp_path: Path) -> None:
    store = factory(tmp_path)
    await store.save(Session(id="s1", owner_id="u1", agent_name="assistant"))

    # Two writers both read v0; the winner lands first.
    loser = await store.get("s1")
    assert loser is not None
    winner = await store.get("s1")
    assert winner is not None
    winner.version += 1
    await store.save(winner, expected_version=0)

    # The loser still holds v0 and must be rejected, not silently overwritten.
    loser.version += 1
    with pytest.raises(SessionVersionConflictError):
        await store.save(loser, expected_version=0)

    loaded = await store.get("s1")
    assert loaded is not None
    assert loaded.version == 1


@pytest.mark.integration
@pytest.mark.parametrize("factory", STORE_FACTORIES, ids=STORE_IDS)
async def test_runtime_run_bumps_session_version_once(factory: Any, tmp_path: Path) -> None:
    store = factory(tmp_path)
    await store.save(Session(id="s1", owner_id="u1", agent_name="assistant"))
    manager = _manager(store, FakeChatModel([ai("done")]))

    handle = await manager.start(RunRequest(session_id="s1", user_id="u1", message="hi"))
    result = await handle.result()
    assert isinstance(result, AIMessage)

    session = await store.get("s1")
    assert session is not None
    assert session.version == 1
    messages = await store.get_messages("s1")
    assert [message.content for message in messages] == ["hi", "done"]


@pytest.mark.integration
@pytest.mark.parametrize("factory", STORE_FACTORIES, ids=STORE_IDS)
async def test_runtime_save_detects_concurrent_bump(factory: Any, tmp_path: Path) -> None:
    """A slower run that read v0 loses to a faster run and fails its CAS save."""
    store = factory(tmp_path)
    await store.save(Session(id="s1", owner_id="u1", agent_name="assistant"))
    slower = _manager(
        store,
        FakeChatModel([ai(tool_calls=[call("sleep")]), ai("from A")]),
        tools=[SleepTool()],
    )
    faster = _manager(store, FakeChatModel([ai("done B")]))

    slower_handle = await slower.start(RunRequest(session_id="s1", user_id="u1", message="A"))
    await asyncio.sleep(0.01)  # let the slower run reach its tool sleep
    faster_handle = await faster.start(RunRequest(session_id="s1", user_id="u1", message="B"))

    await faster_handle.result()  # completes at v0 -> v1
    with pytest.raises(SessionVersionConflictError):
        await slower_handle.result()  # still holds v0; its CAS save must fail

    session = await store.get("s1")
    assert session is not None
    assert session.version == 1


@pytest.mark.integration
async def test_session_lock_serializes_concurrent_runs(tmp_path: Path) -> None:
    store = _memory(tmp_path)
    await store.save(Session(id="s1", owner_id="u1", agent_name="assistant"))
    lock = SessionLockManager()
    first = _manager(
        store,
        FakeChatModel([ai(tool_calls=[call("sleep")]), ai("from A")]),
        tools=[SleepTool()],
        lock=lock,
    )
    second = _manager(
        store,
        FakeChatModel([ai(tool_calls=[call("sleep")]), ai("from B")]),
        tools=[SleepTool()],
        lock=lock,
    )

    first_handle = await first.start(RunRequest(session_id="s1", user_id="u1", message="A"))
    second_handle = await second.start(RunRequest(session_id="s1", user_id="u1", message="B"))

    results = await asyncio.gather(first_handle.result(), second_handle.result())
    assert [result.content for result in results] == ["from A", "from B"]

    # Serialized, not lost: both runs landed, version bumped exactly twice.
    session = await store.get("s1")
    assert session is not None
    assert session.version == 2
    messages = await store.get_messages("s1")
    assert [message.content for message in messages if message.content in ("A", "B")] == [
        "A",
        "B",
    ]
    assert [message.content for message in messages if message.content == "from A"] == ["from A"]
    assert [message.content for message in messages if message.content == "from B"] == ["from B"]


@pytest.mark.integration
async def test_bootstrap_creates_full_object_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-real")  # building a model needs one
    config = AppConfig(workspace=tmp_path / "ws", db_path=tmp_path / "ws" / "index.db")
    app = build_application(config)

    assert isinstance(app, Application)
    assert app.agent.name == "assistant"
    assert app.run_manager is not None
    assert app.session_store is not None
    assert (tmp_path / "ws" / ".agent" / "sessions").is_dir()

    # The wired stores round-trip through the composition root.
    await app.session_store.save(Session(id="s1", owner_id="u1", agent_name="assistant"))
    loaded = await app.session_store.get("s1")
    assert loaded is not None
    assert loaded.id == "s1"
