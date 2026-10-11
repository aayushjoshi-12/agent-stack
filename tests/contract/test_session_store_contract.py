"""A parametrized contract suite for :class:`SessionStore`.

Runs the same assertions against every implementation so behavioural drift is impossible:
the in-memory store, the file conversation store, and the SQLite-backed store.

The fixture takes a *factory* (path -> store) so each backend gets an isolated ``tmp_path``.
"""

from __future__ import annotations

from collections.abc import Callable
from itertools import count
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from agent_stack.core.session import InMemorySessionStore, Session
from agent_stack.persistence.conversation import FileConversationStore
from agent_stack.persistence.sqlite import SqliteDatabase, SqliteSessionStore

_IDS = count(1)


def _session() -> Session:
    return Session(
        id=f"s{next(_IDS):02d}",
        owner_id="u1",
        agent_name="assistant",
        title="contract",
        metadata={"origin": "contract"},
    )


def _in_memory_factory(path: Path) -> Any:
    return InMemorySessionStore()


def _file_factory(path: Path) -> Any:
    return FileConversationStore(path / "agent")


def _sqlite_factory(path: Path) -> Any:
    return SqliteSessionStore(
        SqliteDatabase(path / "index.db"),
        FileConversationStore(path / "agent"),
    )


StoreFactory = Callable[[Path], Any]


@pytest.fixture
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Any:
    factory: StoreFactory = request.param
    return factory(tmp_path)


@pytest.mark.parametrize(
    "store",
    [_in_memory_factory, _file_factory, _sqlite_factory],
    ids=["in-memory", "file", "sqlite"],
    indirect=True,
)
class TestSessionStoreContract:
    async def test_create_and_get_session(self, store: Any) -> None:
        session = _session()

        await store.save(session)

        loaded = await store.get(session.id)
        assert loaded is not None
        assert loaded.id == session.id
        assert loaded.owner_id == session.owner_id
        assert loaded.agent_name == session.agent_name
        assert loaded.title == session.title
        assert loaded.metadata == session.metadata

    async def test_get_unknown_session_returns_none(self, store: Any) -> None:
        assert await store.get("nope") is None

    async def test_append_and_read_messages_preserves_order(self, store: Any) -> None:
        session = _session()
        await store.save(session)
        await store.append_messages(
            session.id,
            [HumanMessage(content="one"), AIMessage(content="two", id="m2")],
        )
        await store.append_messages(
            session.id,
            [SystemMessage(content="three")],
        )

        messages = await store.get_messages(session.id)
        assert isinstance(messages, list)
        assert [message.content for message in messages] == ["one", "two", "three"]

    async def test_get_messages_returns_a_copy(self, store: Any) -> None:
        session = _session()
        await store.save(session)
        await store.append_messages(session.id, [HumanMessage(content="hi")])

        messages = await store.get_messages(session.id)
        messages.append(AIMessage(content="mutated"))

        again: list[BaseMessage] = await store.get_messages(session.id)
        assert len(again) == 1
        assert again[0].content == "hi"

    async def test_get_messages_unknown_session_is_empty(self, store: Any) -> None:
        assert await store.get_messages("nope") == []

    async def test_append_messages_to_unknown_session_still_reads_back(self, store: Any) -> None:
        await store.append_messages("fresh", [HumanMessage(content="hi")])
        assert [message.content for message in await store.get_messages("fresh")] == ["hi"]

    async def test_delete_removes_session_and_messages(self, store: Any) -> None:
        session = _session()
        await store.save(session)
        await store.append_messages(session.id, [HumanMessage(content="hi")])

        await store.delete(session.id)

        assert await store.get(session.id) is None
        assert await store.get_messages(session.id) == []

    async def test_delete_unknown_session_is_a_noop(self, store: Any) -> None:
        await store.delete("nope")  # must not raise


async def test_file_store_is_also_a_conversation_store(tmp_path: Path) -> None:
    """The file store satisfies both protocols, so it can be dropped into AgentRuntime."""
    store = _file_factory(tmp_path)
    assert isinstance(store, FileConversationStore)
    session_dir = store.session_dir("s01")
    assert str(session_dir).startswith(str(tmp_path))
