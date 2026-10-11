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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from langchain_core.messages import BaseMessage

from agent_stack.core.run import Run, RunStatus
from agent_stack.core.session import Session, SessionStore
from agent_stack.persistence.conversation import ConversationStore
from agent_stack.persistence.migrations import create_schema

_RUN_COLUMNS = (
    "id, session_id, parent_run_id, status, step_count, tool_call_count, "
    "started_at, finished_at, error, metadata_json"
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

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        create_schema(self._conn)

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        await asyncio.to_thread(self._execute_sync, sql, params)

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
                status=excluded.status,
                step_count=excluded.step_count,
                tool_call_count=excluded.tool_call_count,
                error=excluded.error
            """,
            (
                run.id,
                run.session_id,
                None,
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

    async def list_incomplete(self) -> list[Run]:
        """Return runs that are still pending or running (recovery dropdown for P11)."""
        rows = await self._db.query(
            "SELECT * FROM runs WHERE status IN (?, ?) ORDER BY id",
            (RunStatus.PENDING.value, RunStatus.RUNNING.value),
        )
        return [_row_to_run(row) for row in rows]


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

    def __init__(self, db: SqliteDatabase, conversation: ConversationBackend) -> None:
        self._db = db
        self._conversation = conversation

    async def get(self, session_id: str) -> Session | None:
        rows = await self._db.query("SELECT * FROM sessions WHERE id = ?", (session_id,))
        if not rows:
            return None
        row = rows[0]
        metadata_raw = row["metadata_json"] or "{}"
        metadata: Any = json.loads(metadata_raw)
        return Session(
            id=str(row["id"]),
            owner_id=str(row["owner_id"]),
            agent_name=str(row["agent_name"]),
            title=None if row["title"] is None else str(row["title"]),
            status=str(row["status"]),
            version=int(row["version"]),
            metadata=dict(metadata) if isinstance(metadata, dict) else {},
        )

    async def save(self, session: Session) -> None:
        now = _now_iso()
        await self._db.execute(
            """
            INSERT INTO sessions
                (id, owner_id, agent_name, title, status, version,
                 created_at, updated_at, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                owner_id=excluded.owner_id,
                agent_name=excluded.agent_name,
                title=excluded.title,
                status=excluded.status,
                version=excluded.version,
                updated_at=excluded.updated_at,
                metadata_json=excluded.metadata_json
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
            ),
        )

    async def get_messages(self, session_id: str) -> list[BaseMessage]:
        return await self._conversation.read_messages(session_id)

    async def append_messages(self, session_id: str, messages: Sequence[BaseMessage]) -> None:
        await self._conversation.append_messages(session_id, messages)

    async def delete(self, session_id: str) -> None:
        await self._conversation.delete(session_id)
        await self._db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        for table in ("runs", "message_index", "artifacts_index"):
            await self._db.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))


def _row_to_run(row: sqlite3.Row) -> Run:
    error_value = row["error"]
    return Run(
        id=str(row["id"]),
        session_id=str(row["session_id"]),
        status=RunStatus(str(row["status"])),
        step_count=int(row["step_count"]),
        tool_call_count=int(row["tool_call_count"]),
        error=None if error_value is None else str(error_value),
    )
