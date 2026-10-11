"""Integration tests for the file-based conversation store.

Everything runs on ``tmp_path`` — never the real home or workspace. The acceptance tests
from ``docs/design/conversation-storage.md`` section 8: JSONL round-trips, restart
simulation, Markdown completeness, and directory deletion.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agent_stack.core.session import Session
from agent_stack.persistence.conversation import FileConversationStore
from agent_stack.persistence.serializers import message_to_record


@pytest.fixture
def store(tmp_path: Path) -> FileConversationStore:
    return FileConversationStore(tmp_path / "agent")


@pytest.mark.integration
async def test_transcript_jsonl_roundtrips_messages(store: FileConversationStore) -> None:
    session_id = "s-roundtrip"
    await store.save(Session(id=session_id, owner_id="u1", agent_name="assistant"))
    await store.append_messages(
        session_id,
        [
            HumanMessage(content="inspect the repo"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call-1",
                        "name": "terminal",
                        "args": {"command": "ls"},
                        "type": "tool_call",
                    }
                ],
            ),
            ToolMessage(content="12 passed", tool_call_id="call-1"),
            AIMessage(content="All 12 tests passed."),
        ],
    )

    messages = await store.get_messages(session_id)

    assert [message.content for message in messages] == [
        "inspect the repo",
        "",
        "12 passed",
        "All 12 tests passed.",
    ]
    assert messages[1].tool_calls == [
        {"id": "call-1", "name": "terminal", "args": {"command": "ls"}, "type": "tool_call"}
    ]
    assert messages[2].tool_call_id == "call-1"
    assert isinstance(messages[0], HumanMessage)
    assert isinstance(messages[3], AIMessage)


@pytest.mark.integration
async def test_second_store_instance_reads_same_history(
    store: FileConversationStore,
) -> None:
    """Restart simulation: a fresh store over the same root sees the whole history."""
    session_id = "s-restart"
    await store.save(Session(id=session_id, owner_id="u1", agent_name="assistant"))
    await store.append_messages(session_id, [HumanMessage(content="one")])
    await store.append_messages(session_id, [AIMessage(content="two")])

    reopened = FileConversationStore(store.session_dir(session_id).parents[1])

    assert [message.content for message in await reopened.get_messages(session_id)] == [
        "one",
        "two",
    ]


@pytest.mark.integration
async def test_transcript_md_contains_full_chat(store: FileConversationStore) -> None:
    session_id = "s-md"
    await store.save(Session(id=session_id, owner_id="u1", agent_name="assistant"))
    await store.append_messages(
        session_id,
        [
            HumanMessage(content="inspect the repo"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call-1",
                        "name": "terminal",
                        "args": {"command": "ls"},
                        "type": "tool_call",
                    }
                ],
            ),
            ToolMessage(content="12 passed", tool_call_id="call-1"),
            AIMessage(content="All 12 tests passed."),
        ],
    )

    markdown = (store.session_dir(session_id) / "transcript.md").read_text(encoding="utf-8")

    assert "inspect the repo" in markdown
    assert "12 passed" in markdown
    assert "All 12 tests passed." in markdown
    assert "terminal" in markdown
    assert "call-1" in markdown
    assert "#" in markdown


@pytest.mark.integration
async def test_append_assigns_monotonic_seq(store: FileConversationStore) -> None:
    session_id = "s-seq"
    await store.append_messages(session_id, [HumanMessage(content="a")])
    await store.append_messages(session_id, [AIMessage(content="b"), SystemMessage(content="c")])

    records = await store.read(session_id)

    assert [record.seq for record in records] == [1, 2, 3]


@pytest.mark.integration
async def test_session_dir_deleted_with_session(store: FileConversationStore) -> None:
    session_id = "s-delete"
    await store.save(Session(id=session_id, owner_id="u1", agent_name="assistant"))
    await store.append_messages(session_id, [HumanMessage(content="bye")])
    session_dir = store.session_dir(session_id)
    assert session_dir.exists()

    await store.delete(session_id)

    assert not session_dir.exists()
    assert await store.get(session_id) is None
    assert await store.get_messages(session_id) == []


@pytest.mark.integration
async def test_metadata_survives_json(store: FileConversationStore) -> None:
    session_id = "s-meta"
    original = Session(
        id=session_id,
        owner_id="u1",
        agent_name="coder",
        title="my chat",
        status="archived",
        version=3,
        metadata={"theme": "dark"},
    )

    await store.save(original)
    loaded = await store.get(session_id)

    assert loaded == original


@pytest.mark.integration
async def test_append_via_conversation_protocol_accepts_records(
    store: FileConversationStore,
) -> None:
    session_id = "s-protocol"
    record = message_to_record(
        HumanMessage(content="hello from a record"),
        seq=1,
        run_id="r-run",
        ts=datetime(2026, 10, 11, 12, 0, 0, tzinfo=UTC),
    )

    await store.append(session_id, [record])

    records = await store.read(session_id)
    assert len(records) == 1
    assert records[0].content == "hello from a record"
    assert records[0].seq == 1
    assert records[0] == record
