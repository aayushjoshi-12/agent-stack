"""Durable event log (P29): every event the bus publishes, replayable after a restart.

The log holds event *metadata plus capped, redacted data* (tool args and outputs go through
the same sanitiser as the audit log); full message payloads stay in ``transcript.jsonl``
(ADR-008). Rows are append-only and never updated.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from agent_stack.core.event_bus import EventAuditSink, InMemoryEventBus
from agent_stack.core.events import AgentEvent, EventType
from agent_stack.observability.audit import audit_record
from agent_stack.observability.redact import env_secrets

if TYPE_CHECKING:
    from agent_stack.persistence.sqlite import SqliteDatabase

__all__ = ["EventLogStore", "EventStoreBus", "SqliteEventLogStore"]

_COLUMNS = "id, run_id, session_id, parent_run_id, sequence, type, timestamp, data_json"


class EventLogStore(Protocol):
    """Infrastructure boundary for the durable, append-only event log."""

    async def append(self, events: Sequence[AgentEvent]) -> None: ...

    async def read(
        self,
        *,
        run_id: str | None = None,
        session_id: str | None = None,
        up_to_sequence: int | None = None,
        after_sequence: int | None = None,
    ) -> list[AgentEvent]:
        """Events in publish order. ``up_to_sequence`` keeps events whose (per-run)
        sequence is at most that value, ``after_sequence`` those strictly greater."""
        ...

    async def get(self, event_id: str) -> AgentEvent | None: ...

    async def usage_rows(self, session_id: str) -> list[Mapping[str, Any]]:
        """The data of every ``model.usage`` event of a session, subagent runs included."""
        ...


class SqliteEventLogStore:
    """``EventLogStore`` in the ``event_log`` table of the index database."""

    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db
        self._known = env_secrets()

    async def append(self, events: Sequence[AgentEvent]) -> None:
        rows = []
        for event in events:
            record = audit_record(event, self._known)
            rows.append(
                (
                    event.id,
                    event.run_id,
                    event.session_id,
                    event.parent_run_id,
                    event.sequence,
                    event.type.value,
                    event.timestamp.isoformat(),
                    json.dumps(record["data"], ensure_ascii=False, default=str),
                )
            )
        if rows:
            await self._db.executemany(
                f"INSERT OR IGNORE INTO event_log ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    async def read(
        self,
        *,
        run_id: str | None = None,
        session_id: str | None = None,
        up_to_sequence: int | None = None,
        after_sequence: int | None = None,
    ) -> list[AgentEvent]:
        clauses: list[str] = []
        params: list[object] = []
        for column, value in (("run_id", run_id), ("session_id", session_id)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        if up_to_sequence is not None:
            clauses.append("sequence <= ?")
            params.append(up_to_sequence)
        if after_sequence is not None:
            clauses.append("sequence > ?")
            params.append(after_sequence)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await self._db.query(
            f"SELECT {_COLUMNS} FROM event_log{where} ORDER BY rowid", params
        )
        return [_row_to_event(row) for row in rows]

    async def usage_rows(self, session_id: str) -> list[Mapping[str, Any]]:
        # A subagent runs in its own session but its events carry the parent's run id, so walk
        # from the session's runs down through every descendant run.
        rows = await self._db.query(
            """
            WITH RECURSIVE runs(run_id) AS (
                SELECT DISTINCT run_id FROM event_log WHERE session_id = ?
                UNION
                SELECT DISTINCT e.run_id FROM event_log e JOIN runs r ON e.parent_run_id = r.run_id
            )
            SELECT data_json FROM event_log
            WHERE type = ? AND run_id IN (SELECT run_id FROM runs)
            ORDER BY rowid
            """,
            (session_id, EventType.MODEL_USAGE.value),
        )
        out: list[Mapping[str, Any]] = []
        for row in rows:
            data = json.loads(row["data_json"])
            if isinstance(data, dict):
                out.append(data)
        return out

    async def get(self, event_id: str) -> AgentEvent | None:
        rows = await self._db.query(f"SELECT {_COLUMNS} FROM event_log WHERE id = ?", (event_id,))
        return _row_to_event(rows[0]) if rows else None


def _row_to_event(row: sqlite3.Row) -> AgentEvent:
    data = json.loads(row["data_json"])
    parent = row["parent_run_id"]
    return AgentEvent(
        id=str(row["id"]),
        type=EventType(row["type"]),
        run_id=str(row["run_id"]),
        session_id=str(row["session_id"]),
        sequence=int(row["sequence"]),
        timestamp=datetime.fromisoformat(row["timestamp"]),
        data=data if isinstance(data, dict) else {},
        parent_run_id=None if parent is None else str(parent),
    )


class EventStoreBus(InMemoryEventBus):
    """The in-memory bus that also appends every sequenced event to a durable log.

    Appending happens before subscribers see the event, so anything a consumer observed is
    already replayable. Publishes are serialised so log order equals sequence order.
    """

    def __init__(self, log: EventLogStore, audit: EventAuditSink | None = None) -> None:
        super().__init__(audit=audit)
        self._log = log
        self._publish_lock = asyncio.Lock()

    async def publish(self, event: AgentEvent) -> None:
        async with self._publish_lock:
            await super().publish(event)

    async def _persist(self, event: AgentEvent) -> None:
        await self._log.append([event])

    async def replay(
        self,
        *,
        run_id: str | None = None,
        session_id: str | None = None,
        up_to_sequence: int | None = None,
    ) -> AsyncIterator[AgentEvent]:
        for event in await self._log.read(
            run_id=run_id, session_id=session_id, up_to_sequence=up_to_sequence
        ):
            yield event
