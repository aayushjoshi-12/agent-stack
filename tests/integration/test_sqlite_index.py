"""Integration tests for the SQLite index.

The hard invariant from `docs/design/conversation-storage.md`: SQLite holds **no** message
content and **no** artifact bytes. The conversation lives in files; SQLite only indexes it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent_stack.core.run import Run, RunStatus
from agent_stack.core.session import Session
from agent_stack.persistence.conversation import FileConversationStore
from agent_stack.persistence.sqlite import (
    SqliteDatabase,
    SqliteIndex,
    SqliteRunStore,
    SqliteSessionStore,
)

EXPECTED_COLUMNS: dict[str, set[str]] = {
    "sessions": {
        "id",
        "owner_id",
        "agent_name",
        "title",
        "status",
        "version",
        "created_at",
        "updated_at",
        "metadata_json",
    },
    "runs": {
        "id",
        "session_id",
        "parent_run_id",
        "status",
        "step_count",
        "tool_call_count",
        "started_at",
        "finished_at",
        "error",
        "metadata_json",
    },
    "message_index": {
        "id",
        "session_id",
        "run_id",
        "seq",
        "kind",
        "jsonl_offset",
        "created_at",
    },
    "artifacts_index": {
        "id",
        "session_id",
        "run_id",
        "name",
        "rel_path",
        "mime_type",
        "size_bytes",
        "created_at",
    },
}

# Occurring in a column name would mean the payload leaks into SQLite.
# (``metadata_json`` is allowed — it is session *metadata*, not conversation content.)
FORBIDDEN_TOKENS = ("content", "payload", "body", "blob", "message", "transcript")


@pytest.fixture
def db(tmp_path: Path) -> SqliteDatabase:
    return SqliteDatabase(tmp_path / "index.db")


@pytest.fixture
def conversation(tmp_path: Path) -> FileConversationStore:
    return FileConversationStore(tmp_path / "agent")


@pytest.mark.integration
def test_schema_is_created(db: SqliteDatabase) -> None:
    tables = db._conn.execute(  # type: ignore[attr-defined]
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    names = {row[0] for row in tables}
    assert EXPECTED_COLUMNS.keys() <= names


@pytest.mark.integration
def test_sqlite_has_no_message_payload_columns(db: SqliteDatabase) -> None:
    conn = db._conn  # type: ignore[attr-defined]
    for table, expected in EXPECTED_COLUMNS.items():
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        columns = {str(row[1]): str(row[2]).upper() for row in rows}
        assert set(columns) == expected, f"{table} column set drifted"
        for name, declared_type in columns.items():
            assert declared_type != "BLOB", f"{table}.{name} is a BLOB column"
            lowered = name.lower()
            for token in FORBIDDEN_TOKENS:
                assert token not in lowered, f"{table}.{name} looks like a payload column"


@pytest.mark.integration
async def test_run_store_round_trips_and_finds_incomplete(db: SqliteDatabase) -> None:
    store = SqliteRunStore(db)
    run = Run(
        id="r1",
        session_id="s1",
        status=RunStatus.RUNNING,
        step_count=2,
        tool_call_count=1,
    )

    await store.save(run)

    loaded = await store.get("r1")
    assert loaded is not None
    assert loaded.id == "r1"
    assert loaded.session_id == "s1"
    assert loaded.status == RunStatus.RUNNING
    assert loaded.step_count == 2
    assert loaded.tool_call_count == 1


@pytest.mark.integration
async def test_saving_a_run_again_updates_it(db: SqliteDatabase) -> None:
    store = SqliteRunStore(db)
    await store.save(
        Run(id="r1", session_id="s1", status=RunStatus.RUNNING, step_count=1, tool_call_count=0)
    )
    await store.save(
        Run(id="r1", session_id="s1", status=RunStatus.COMPLETED, step_count=4, tool_call_count=2)
    )

    loaded = await store.get("r1")
    assert loaded is not None
    assert loaded.status == RunStatus.COMPLETED
    assert loaded.step_count == 4


@pytest.mark.integration
async def test_list_incomplete_finds_running_runs(db: SqliteDatabase) -> None:
    store = SqliteRunStore(db)
    await store.save(Run(id="r1", session_id="s1", status=RunStatus.PENDING))
    await store.save(Run(id="r2", session_id="s1", status=RunStatus.RUNNING))
    await store.save(Run(id="r3", session_id="s1", status=RunStatus.COMPLETED))

    incomplete = await store.list_incomplete()

    assert {run.id for run in incomplete} == {"r1", "r2"}
    assert incomplete[0].status in (RunStatus.PENDING, RunStatus.RUNNING)


@pytest.mark.integration
async def test_get_unknown_run_returns_none(db: SqliteDatabase) -> None:
    store = SqliteRunStore(db)
    assert await store.get("missing") is None


@pytest.mark.integration
async def test_sqlite_session_store_metadata_round_trip(
    db: SqliteDatabase,
    conversation: FileConversationStore,
) -> None:
    store = SqliteSessionStore(db, conversation)
    session = Session(
        id="s1",
        owner_id="u1",
        agent_name="coder",
        title="title",
        status="active",
        version=2,
        metadata={"theme": "dark"},
    )

    await store.save(session)
    loaded = await store.get("s1")

    assert loaded == session


@pytest.mark.integration
async def test_sqlite_session_store_messages_delegate_to_files(
    db: SqliteDatabase,
    conversation: FileConversationStore,
) -> None:
    store = SqliteSessionStore(db, conversation)
    await store.save(Session(id="s1", owner_id="u1", agent_name="assistant"))

    await store.append_messages(
        "s1", [HumanMessage(content="one"), AIMessage(content="two", id="m2")]
    )

    messages = await store.get_messages("s1")
    assert [message.content for message in messages] == ["one", "two"]

    # The transcript file (not SQLite) is the source of truth.
    direct = await conversation.read_messages("s1")
    assert [message.content for message in direct] == ["one", "two"]
    transcript = conversation.session_dir("s1") / "transcript.jsonl"
    assert transcript.exists()


@pytest.mark.integration
async def test_sqlite_session_store_delete_cleans_rows_and_files(
    db: SqliteDatabase,
    conversation: FileConversationStore,
) -> None:
    store = SqliteSessionStore(db, conversation)
    await store.save(Session(id="s1", owner_id="u1", agent_name="assistant"))
    await store.append_messages("s1", [HumanMessage(content="hi")])
    index = SqliteIndex(db)
    await index.record_message(session_id="s1", run_id="r1", seq=1, kind="human", jsonl_offset=0)

    await store.delete("s1")

    assert await store.get("s1") is None
    assert await conversation.read("s1") == []
    assert not conversation.session_dir("s1").exists()
    remaining_sessions = await db.query("SELECT COUNT(*) AS n FROM sessions")
    remaining_messages = await db.query("SELECT COUNT(*) AS n FROM message_index")
    assert remaining_sessions[0]["n"] == 0
    assert remaining_messages[0]["n"] == 0


@pytest.mark.integration
async def test_sqlite_index_records_and_reads_message_rows(db: SqliteDatabase) -> None:
    index = SqliteIndex(db)
    await index.record_message(session_id="s1", run_id="r1", seq=1, kind="human", jsonl_offset=0)
    await index.record_message(session_id="s1", run_id="r1", seq=2, kind="ai", jsonl_offset=89)
    await index.record_message(session_id="s2", run_id="r9", seq=1, kind="tool", jsonl_offset=12)

    for_session = await index.list_messages("s1")

    assert [(entry.seq, entry.kind) for entry in for_session] == [(1, "human"), (2, "ai")]
    assert for_session[1].jsonl_offset == 89
    assert all(entry.session_id == "s1" for entry in for_session)


@pytest.mark.integration
async def test_sqlite_index_records_and_reads_artifact_rows(db: SqliteDatabase) -> None:
    index = SqliteIndex(db)
    await index.record_artifact(
        session_id="s1",
        run_id="r1",
        name="patch.diff",
        rel_path="s1/artifacts/a1__patch.diff",
        mime_type="text/x-diff",
        size_bytes=2048,
    )

    artifacts = await index.list_artifacts("s1")

    assert len(artifacts) == 1
    assert artifacts[0].name == "patch.diff"
    assert artifacts[0].size_bytes == 2048
    assert artifacts[0].rel_path == "s1/artifacts/a1__patch.diff"


@pytest.mark.integration
def test_database_creates_parent_directories(tmp_path: Path) -> None:
    db = SqliteDatabase(tmp_path / "nested" / "deep" / "index.db")
    assert (tmp_path / "nested" / "deep" / "index.db").exists()
    db.close()


@pytest.mark.integration
def test_database_is_reopenable(db: SqliteDatabase, tmp_path: Path) -> None:
    db.close()
    reopened = SqliteDatabase(tmp_path / "index.db")
    conn = reopened._conn  # type: ignore[attr-defined]
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    assert any(row[0] == "sessions" for row in tables)
    reopened.close()


def test_expected_columns_are_sane() -> None:
    """Guard the test's own expectations: no payload columns sneak into the spec."""
    for _table, columns in EXPECTED_COLUMNS.items():
        for column in columns:
            lowered = column.lower()
            assert not any(token in lowered for token in FORBIDDEN_TOKENS)
            assert "bytes" not in lowered or lowered == "size_bytes"
