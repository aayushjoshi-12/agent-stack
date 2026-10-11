"""Unit tests for the in-memory session store.

The store must never leak internal state: `get_messages` returns a copy, and reads for an
unknown session return `None` / `[]` rather than raising.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage

from agent_stack.core.session import InMemorySessionStore, Session


def _session(session_id: str = "s1") -> Session:
    return Session(id=session_id, owner_id="u1", agent_name="assistant")


async def test_save_get_round_trip() -> None:
    store = InMemorySessionStore()
    session = _session()

    await store.save(session)

    loaded = await store.get("s1")
    assert loaded == session


async def test_get_messages_returns_copy() -> None:
    store = InMemorySessionStore()
    await store.append_messages("s1", [HumanMessage(content="hi")])

    messages = await store.get_messages("s1")
    messages.append(AIMessage(content="mutated"))

    again = await store.get_messages("s1")
    assert len(again) == 1
    assert again[0].content == "hi"


async def test_messages_accumulate_across_appends() -> None:
    store = InMemorySessionStore()

    await store.append_messages("s1", [HumanMessage(content="one")])
    await store.append_messages("s1", [AIMessage(content="two")])

    messages = await store.get_messages("s1")
    assert [message.content for message in messages] == ["one", "two"]


async def test_delete_clears_session_and_messages() -> None:
    store = InMemorySessionStore()
    await store.save(_session())
    await store.append_messages("s1", [HumanMessage(content="hi")])

    await store.delete("s1")

    assert await store.get("s1") is None
    assert await store.get_messages("s1") == []


async def test_unknown_session_returns_none_and_empty() -> None:
    store = InMemorySessionStore()

    assert await store.get("nope") is None
    assert await store.get_messages("nope") == []
