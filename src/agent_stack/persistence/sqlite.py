"""SQLite-backed stores.

SQLite holds session metadata, run lifecycle and lightweight index rows (message
*pointers* and artifact metadata). It never holds message content or artifact bytes — those
live in the per-session transcript files. The connection is wrapped with
``asyncio.to_thread`` so store methods are awaitable without pulling in an async driver.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from langchain_core.messages import BaseMessage

from agent_stack.core.errors import PersistenceError, SessionVersionConflictError
from agent_stack.core.run import LIVE_RUN_STATUSES, Run, RunStatus
from agent_stack.core.session import Session, SessionStore
from agent_stack.core.state import Checkpoint, CheckpointStatus
from agent_stack.persistence.conversation import ConversationStore
from agent_stack.persistence.event_log import EventLogStore
from agent_stack.persistence.migrations import create_schema
from agent_stack.persistence.serializers import MessageRecord
from agent_stack.tools.executor import ToolExecutionResult

_RUN_COLUMNS = (
    "id, session_id, parent_run_id, status, step_count, tool_call_count, "
    "started_at, finished_at, error, metadata_json"
)


#: Tables whose rows belong to one session (``session_id``), besides ``sessions`` itself.
SESSION_TABLES = (
    "runs",
    "message_index",
    "artifacts_index",
    "checkpoints",
    "todos",
    "event_log",
    "chat_fts",  # the search index's copy of its text (P32)
    "chat_fts_state",
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class MessageIndexEntry:
    """A pointer into ``transcript.jsonl`` (metadata only, no content)."""

    id: int
    session_id: str
    run_id: str | None
    seq: int
    kind: str
    jsonl_offset: int
    created_at: str


@dataclass(frozen=True)
class ArtifactIndexEntry:
    """Metadata about an artifact file, keyed by its relative path."""

    id: int
    session_id: str
    run_id: str | None
    name: str
    rel_path: str
    mime_type: str | None
    size_bytes: int
    created_at: str


class SqliteDatabase:
    """A tiny async-friendly wrapper around a single ``sqlite3`` connection."""

    def __init__(
        self,
        path: Path | str,
        *,
        factory: type[sqlite3.Connection] | None = None,
    ) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # ``factory`` is sqlite3's standard extension point (tests count commits with a
        # subclass); None means the default Connection class.
        self._conn = sqlite3.connect(
            self._path, check_same_thread=False, factory=factory or sqlite3.Connection
        )
        self._conn.row_factory = sqlite3.Row
        create_schema(self._conn)

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        await asyncio.to_thread(self._execute_sync, sql, params)

    async def execute_rowcount(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Execute a statement and return its affected-row count (for CAS writes)."""
        return await asyncio.to_thread(self._execute_rowcount_sync, sql, params)

    async def executemany(self, sql: str, seq_of_params: Sequence[Sequence[Any]]) -> None:
        await asyncio.to_thread(self._executemany_sync, sql, seq_of_params)

    async def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return await asyncio.to_thread(self._query_sync, sql, params)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _execute_sync(self, sql: str, params: Sequence[Any]) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def _execute_rowcount_sync(self, sql: str, params: Sequence[Any]) -> int:
        with self._lock:
            cursor = self._conn.execute(sql, params)
            self._conn.commit()
            return cursor.rowcount

    def _executemany_sync(self, sql: str, seq_of_params: Sequence[Sequence[Any]]) -> None:
        with self._lock:
            self._conn.executemany(sql, seq_of_params)
            self._conn.commit()

    def _query_sync(self, sql: str, params: Sequence[Any]) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())


class SqliteRunStore:
    """Persist run lifecycle rows in the ``runs`` table."""

    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db

    async def save(self, run: Run) -> None:
        now = _now_iso()
        await self._db.execute(
            f"""
            INSERT INTO runs ({_RUN_COLUMNS})
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                session_id=excluded.session_id,
                parent_run_id=excluded.parent_run_id,
                status=excluded.status,
                step_count=excluded.step_count,
                tool_call_count=excluded.tool_call_count,
                error=excluded.error
            """,
            (
                run.id,
                run.session_id,
                run.parent_run_id,
                run.status.value,
                run.step_count,
                run.tool_call_count,
                now,
                None,
                run.error,
                "{}",
            ),
        )

    async def get(self, run_id: str) -> Run | None:
        rows = await self._db.query("SELECT * FROM runs WHERE id = ?", (run_id,))
        if not rows:
            return None
        return _row_to_run(rows[0])

    async def list_runs(self, parent_run_id: str) -> list[Run]:
        """Return the child runs spawned by ``parent_run_id`` (subagent listing)."""
        rows = await self._db.query(
            "SELECT * FROM runs WHERE parent_run_id = ? ORDER BY id",
            (parent_run_id,),
        )
        return [_row_to_run(row) for row in rows]

    async def list_incomplete(self) -> list[Run]:
        """Return runs that are still pending, running or awaiting approval (for recovery)."""
        statuses = sorted(status.value for status in LIVE_RUN_STATUSES)
        marks = ", ".join("?" for _ in statuses)
        rows = await self._db.query(
            f"SELECT * FROM runs WHERE status IN ({marks}) ORDER BY id", tuple(statuses)
        )
        return [_row_to_run(row) for row in rows]


class SqliteCheckpointStore:
    """Persist run checkpoints and idempotent tool-execution records.

    Both tables hold run *state* metadata only — never conversation content.
    """

    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db

    async def save(self, checkpoint: Checkpoint) -> None:
        await self._db.execute(
            """
            INSERT INTO checkpoints (run_id, session_id, step, tool_call_count, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                checkpoint.run_id,
                checkpoint.session_id,
                checkpoint.step,
                checkpoint.tool_call_count,
                checkpoint.status.value,
                checkpoint.created_at,
            ),
        )

    async def get_latest(self, run_id: str) -> Checkpoint | None:
        rows = await self._db.query(
            "SELECT * FROM checkpoints WHERE run_id = ? ORDER BY id DESC LIMIT 1",
            (run_id,),
        )
        if not rows:
            return None
        return _row_to_checkpoint(rows[0])

    async def record_tool_executions(
        self, run_id: str, results: Sequence[ToolExecutionResult]
    ) -> None:
        """Persist a whole tool batch in one transaction.

        ``(run_id, tool_call_id)`` is the idempotency key, so ``INSERT OR IGNORE``
        keeps a completed tool from ever being recorded twice. One ``executemany``
        call means one commit for the entire batch (see ``SqliteDatabase``).
        """
        rows = [
            (
                run_id,
                result.tool_call_id,
                result.tool_name,
                "success" if result.success else "error",
                result.error,
                _now_iso(),
            )
            for result in results
        ]
        await self._db.executemany(
            """
            INSERT OR IGNORE INTO tool_executions
                (run_id, tool_call_id, tool_name, status, error, ts)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

    async def completed_tool_call_ids(self, run_id: str) -> set[str]:
        """Every ``tool_call_id`` already recorded for a run (the idempotency set)."""
        rows = await self._db.query(
            "SELECT tool_call_id FROM tool_executions WHERE run_id = ?",
            (run_id,),
        )
        return {str(row["tool_call_id"]) for row in rows}


class SqliteIndex:
    """Read/write the ``message_index`` and ``artifacts_index`` tables (metadata only)."""

    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db

    async def record_message(
        self,
        *,
        session_id: str,
        run_id: str | None,
        seq: int,
        kind: str,
        jsonl_offset: int,
        created_at: str | None = None,
    ) -> None:
        await self._db.execute(
            """
            INSERT INTO message_index
                (session_id, run_id, seq, kind, jsonl_offset, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (session_id, run_id, seq, kind, jsonl_offset, created_at or _now_iso()),
        )

    async def list_messages(self, session_id: str) -> list[MessageIndexEntry]:
        rows = await self._db.query(
            "SELECT * FROM message_index WHERE session_id = ? ORDER BY seq",
            (session_id,),
        )
        return [
            MessageIndexEntry(
                id=int(row["id"]),
                session_id=str(row["session_id"]),
                run_id=row["run_id"],
                seq=int(row["seq"]),
                kind=str(row["kind"]),
                jsonl_offset=int(row["jsonl_offset"]),
                created_at=str(row["created_at"]),
            )
            for row in rows
        ]

    async def record_artifact(
        self,
        *,
        session_id: str,
        run_id: str | None,
        name: str,
        rel_path: str,
        mime_type: str | None,
        size_bytes: int,
        created_at: str | None = None,
    ) -> None:
        await self._db.execute(
            """
            INSERT INTO artifacts_index
                (session_id, run_id, name, rel_path, mime_type, size_bytes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                run_id,
                name,
                rel_path,
                mime_type,
                size_bytes,
                created_at or _now_iso(),
            ),
        )

    async def list_artifacts(self, session_id: str) -> list[ArtifactIndexEntry]:
        rows = await self._db.query(
            "SELECT * FROM artifacts_index WHERE session_id = ? ORDER BY id",
            (session_id,),
        )
        return [
            ArtifactIndexEntry(
                id=int(row["id"]),
                session_id=str(row["session_id"]),
                run_id=row["run_id"],
                name=str(row["name"]),
                rel_path=str(row["rel_path"]),
                mime_type=row["mime_type"],
                size_bytes=int(row["size_bytes"]),
                created_at=str(row["created_at"]),
            )
            for row in rows
        ]


class ConversationBackend(ConversationStore, SessionStore, Protocol):
    """A conversation store that also serves session metadata and message appends.

    :class:`FileConversationStore` satisfies both protocols; this alias documents what
    :class:`SqliteSessionStore` requires of its injected conversation store.
    """


class SqliteSessionStore:
    """``SessionStore`` for metadata only; messages delegate to a ``ConversationStore``.

    Files are the source of truth for the conversation, so ``get_messages`` / ``append_messages``
    pass straight through to the injected conversation store.
    """

    def __init__(
        self,
        db: SqliteDatabase,
        conversation: ConversationBackend,
        event_log: EventLogStore | None = None,
    ) -> None:
        self._db = db
        self._conversation = conversation
        self._event_log = event_log

    async def branch(
        self,
        session_id: str,
        branch_point_event_id: str,
        *,
        agent_name: str | None = None,
    ) -> Session:
        """Fork ``session_id`` at a logged event (P30).

        The branch gets a copy of every transcript record written at or before the event's
        timestamp. Messages are persisted when a run ends, so an event inside a run forks
        the conversation as it stood *before* that run's turn; ``run.completed`` and later
        events include it. The copy is a new session directory: neither side writes into
        the other afterwards.
        """
        parent = await self.get(session_id)
        if parent is None:
            raise PersistenceError(f"cannot branch unknown session {session_id}")
        event = (
            None if self._event_log is None else await self._event_log.get(branch_point_event_id)
        )
        if event is None or event.session_id != session_id:
            raise PersistenceError(
                f"event {branch_point_event_id} is not in the log of session {session_id}"
            )
        records = [r for r in await self._conversation.read(session_id) if r.ts <= event.timestamp]
        branch = Session(
            id=f"{session_id}-b{uuid4().hex[:6]}",
            owner_id=parent.owner_id,
            agent_name=agent_name or parent.agent_name,
            title=f"{parent.title or 'Untitled'} (branch)",
            metadata=dict(parent.metadata),
            branch_of=session_id,
            branch_point_event_id=branch_point_event_id,
        )
        await self._conversation.append(branch.id, records)
        self._conversation.copy_images(session_id, branch.id, records)
        await self.save(branch)
        return branch

    async def branch_before_seq(self, session_id: str, seq: int) -> Session:
        """Fork ``session_id`` just before transcript line ``seq`` (no event log needed).

        For conversations whose turns were never logged as events: the branch gets every line
        before ``seq``. The fork point's time is kept in ``metadata["branch_point_ts"]``.
        """
        parent = await self.get(session_id)
        if parent is None:
            raise PersistenceError(f"cannot branch unknown session {session_id}")
        records = await self._conversation.read(session_id)
        at = next((r for r in records if r.seq == seq), None)
        if at is None and not (records and seq > max(r.seq for r in records)):
            raise PersistenceError(f"{session_id} has no transcript line {seq}")
        keep = [r for r in records if r.seq < seq]
        metadata = dict(parent.metadata)
        # a point past the last line (fork the whole thing) is "just after" that line
        point = at.ts if at is not None else max(r.ts for r in records) + timedelta(microseconds=1)
        metadata["branch_point_ts"] = point.isoformat()
        branch = Session(
            id=f"{session_id}-b{uuid4().hex[:6]}",
            owner_id=parent.owner_id,
            agent_name=parent.agent_name,
            title=f"{parent.title or 'Untitled'} (branch)",
            metadata=metadata,
            branch_of=session_id,
            branch_point_event_id=f"seq:{seq}",
        )
        await self._conversation.append(branch.id, keep)
        self._conversation.copy_images(session_id, branch.id, keep)
        await self.save(branch)
        return branch

    async def get(self, session_id: str) -> Session | None:
        rows = await self._db.query("SELECT * FROM sessions WHERE id = ?", (session_id,))
        return _row_to_session(rows[0]) if rows else None

    async def list_all(self, limit: int = 500) -> list[Session]:
        """Every session of every owner, newest first (for the branch tree and the CLI)."""
        rows = await self._db.query(
            "SELECT * FROM sessions ORDER BY updated_at DESC, rowid DESC LIMIT ?", (limit,)
        )
        return [_row_to_session(row) for row in rows]

    async def records(self, session_id: str) -> list[MessageRecord]:
        """The raw transcript lines of a session, each with its timestamp."""
        return await self._conversation.read(session_id)

    async def list_for_owner(self, owner_id: str, limit: int = 50) -> list[Session]:
        rows = await self._db.query(
            "SELECT * FROM sessions WHERE owner_id = ? ORDER BY updated_at DESC, rowid DESC LIMIT ?",
            (owner_id, limit),
        )
        return [_row_to_session(row) for row in rows]

    async def save(self, session: Session, *, expected_version: int | None = None) -> None:
        now = _now_iso()
        if expected_version is None:
            # Unconditional upsert (creation path and legacy callers).
            await self._db.execute(
                """
                INSERT INTO sessions
                    (id, owner_id, agent_name, title, status, version,
                     created_at, updated_at, metadata_json, branch_of, branch_point_event_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    owner_id=excluded.owner_id,
                    agent_name=excluded.agent_name,
                    title=excluded.title,
                    status=excluded.status,
                    version=excluded.version,
                    updated_at=excluded.updated_at,
                    metadata_json=excluded.metadata_json,
                    branch_of=excluded.branch_of,
                    branch_point_event_id=excluded.branch_point_event_id
                """,
                (
                    session.id,
                    session.owner_id,
                    session.agent_name,
                    session.title,
                    session.status,
                    session.version,
                    now,
                    now,
                    json.dumps(dict(session.metadata), ensure_ascii=False),
                    session.branch_of,
                    session.branch_point_event_id,
                ),
            )
            return

        # Optimistic compare-and-swap: only a writer whose expected version still matches
        # the stored one may land. rowcount == 0 means a concurrent writer won first.
        updated = await self._db.execute_rowcount(
            """
            UPDATE sessions SET
                owner_id=?, agent_name=?, title=?, status=?, version=?,
                updated_at=?, metadata_json=?, branch_of=?, branch_point_event_id=?
            WHERE id = ? AND version = ?
            """,
            (
                session.owner_id,
                session.agent_name,
                session.title,
                session.status,
                session.version,
                now,
                json.dumps(dict(session.metadata), ensure_ascii=False),
                session.branch_of,
                session.branch_point_event_id,
                session.id,
                expected_version,
            ),
        )
        if updated == 1:
            return

        # A brand-new session (stored version 0) is insertable only when the expected
        # version is also 0; any other miss is a genuine lost update.
        if expected_version == 0:
            existing = await self._db.query(
                "SELECT version FROM sessions WHERE id = ?", (session.id,)
            )
            if not existing:
                await self._db.execute(
                    """
                    INSERT INTO sessions
                        (id, owner_id, agent_name, title, status, version,
                         created_at, updated_at, metadata_json,
                         branch_of, branch_point_event_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session.id,
                        session.owner_id,
                        session.agent_name,
                        session.title,
                        session.status,
                        session.version,
                        now,
                        now,
                        json.dumps(dict(session.metadata), ensure_ascii=False),
                        session.branch_of,
                        session.branch_point_event_id,
                    ),
                )
                return
        raise SessionVersionConflictError(
            f"session {session.id} version mismatch: expected {expected_version} "
            f"(a concurrent writer changed the session first)"
        )

    async def get_messages(self, session_id: str) -> list[BaseMessage]:
        return await self._conversation.read_messages(session_id)

    async def image(self, session_id: str, name: str) -> bytes | None:
        """One of the session's stored images (P44), by its content-addressed file name."""
        return await self._conversation.image(session_id, name)

    async def append_messages(self, session_id: str, messages: Sequence[BaseMessage]) -> None:
        await self._conversation.append_messages(session_id, messages)

    async def delete(self, session_id: str) -> None:
        """Delete a chat and its subagents' sessions (rows, and a pre-P46 flat folder)."""
        for member in [session_id, *await self.subagent_sessions(session_id)]:
            await self._delete_one(member)

    async def subagent_sessions(self, session_id: str) -> list[str]:
        """Ids of the sessions a chat's subagents ran in: ``<chat>::...``, or before P46
        ``<one of its run ids>::...``."""
        prefixes = [f"{session_id}::"] + [
            f"{row['id']}::"
            for row in await self._db.query(
                "SELECT id FROM runs WHERE session_id = ?", (session_id,)
            )
        ]
        found: set[str] = set()
        for prefix in prefixes:
            for table, column in (("sessions", "id"), ("event_log", "session_id")):
                rows = await self._db.query(
                    f"SELECT DISTINCT {column} AS id FROM {table} WHERE substr({column}, 1, ?) = ?",
                    (len(prefix), prefix),
                )
                found.update(str(row["id"]) for row in rows)
        return sorted(found)

    async def _delete_one(self, session_id: str) -> None:
        await self._conversation.delete(session_id)
        await self._db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        for table in SESSION_TABLES:
            await self._db.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))
        # tool_executions rows belong to runs (no session column): drop the orphans above.
        await self._db.execute(
            "DELETE FROM tool_executions WHERE run_id NOT IN (SELECT id FROM runs)"
        )


def _row_to_session(row: sqlite3.Row) -> Session:
    metadata: Any = json.loads(row["metadata_json"] or "{}")
    return Session(
        id=str(row["id"]),
        owner_id=str(row["owner_id"]),
        agent_name=str(row["agent_name"]),
        title=None if row["title"] is None else str(row["title"]),
        status=str(row["status"]),
        version=int(row["version"]),
        metadata=dict(metadata) if isinstance(metadata, dict) else {},
        branch_of=row["branch_of"],
        branch_point_event_id=row["branch_point_event_id"],
        created_at=None if row["created_at"] is None else str(row["created_at"]),
        updated_at=None if row["updated_at"] is None else str(row["updated_at"]),
    )


def _row_to_run(row: sqlite3.Row) -> Run:
    error_value = row["error"]
    parent_run_id_value = row["parent_run_id"]
    return Run(
        id=str(row["id"]),
        session_id=str(row["session_id"]),
        status=RunStatus(str(row["status"])),
        step_count=int(row["step_count"]),
        tool_call_count=int(row["tool_call_count"]),
        error=None if error_value is None else str(error_value),
        parent_run_id=None if parent_run_id_value is None else str(parent_run_id_value),
    )


def _row_to_checkpoint(row: sqlite3.Row) -> Checkpoint:
    return Checkpoint(
        run_id=str(row["run_id"]),
        session_id=str(row["session_id"]),
        step=int(row["step"]),
        tool_call_count=int(row["tool_call_count"]),
        status=CheckpointStatus(str(row["status"])),
        created_at=str(row["created_at"]),
    )
