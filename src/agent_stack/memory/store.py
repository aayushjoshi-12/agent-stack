"""Persistent, explicitly managed facts that outlive a session (P21).

The model decides what to keep with ``remember`` / ``forget``; nothing is extracted
automatically and there are no embeddings: recall is a case-insensitive keyword match over
keys and values. A *namespace* isolates one owner's facts from another's.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from agent_stack.memory.models import MemoryItem
from agent_stack.persistence.sqlite import SqliteDatabase

__all__ = ["MemoryStore", "SqliteMemoryStore"]


class MemoryStore(Protocol):
    """Infrastructure boundary for persistent memory."""

    async def remember(self, namespace: str, key: str, value: str) -> MemoryItem: ...

    async def get(self, namespace: str, key: str) -> MemoryItem | None: ...

    async def recall(self, namespace: str, query: str, limit: int = 10) -> list[MemoryItem]: ...

    async def forget(self, namespace: str, key: str) -> bool: ...

    async def list_items(self, namespace: str, limit: int = 100) -> list[MemoryItem]: ...


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _like(term: str) -> str:
    """A LIKE pattern matching ``term`` literally (``%``, ``_`` and ``\\`` escaped)."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class SqliteMemoryStore:
    """:class:`MemoryStore` over the ``memory_items`` table."""

    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db

    async def remember(self, namespace: str, key: str, value: str) -> MemoryItem:
        """Store or update a fact; updating keeps the original ``created_at``."""
        now = _now()
        await self._db.execute(
            """
            INSERT INTO memory_items (namespace, key, value, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(namespace, key) DO UPDATE SET
                value=excluded.value, updated_at=excluded.updated_at
            """,
            (namespace, key, value, now, now),
        )
        item = await self.get(namespace, key)
        assert item is not None
        return item

    async def get(self, namespace: str, key: str) -> MemoryItem | None:
        rows = await self._db.query(
            "SELECT * FROM memory_items WHERE namespace = ? AND key = ?", (namespace, key)
        )
        return _row_to_item(rows[0]) if rows else None

    async def recall(self, namespace: str, query: str, limit: int = 10) -> list[MemoryItem]:
        """Items whose key or value contains *every* word of ``query``, newest first.

        An empty query lists the namespace (so ``recall("")`` means "what do you know?").
        """
        terms = query.split()
        clause = " AND ".join("lower(key || ' ' || value) LIKE lower(?) ESCAPE '\\'" for _ in terms)
        sql = "SELECT * FROM memory_items WHERE namespace = ?"
        if clause:
            sql += f" AND {clause}"
        sql += " ORDER BY updated_at DESC, rowid DESC LIMIT ?"
        rows = await self._db.query(sql, (namespace, *(_like(t) for t in terms), limit))
        return [_row_to_item(row) for row in rows]

    async def forget(self, namespace: str, key: str) -> bool:
        """Delete a fact; ``False`` when there was nothing to forget."""
        removed = await self._db.execute_rowcount(
            "DELETE FROM memory_items WHERE namespace = ? AND key = ?", (namespace, key)
        )
        return removed > 0

    async def list_items(self, namespace: str, limit: int = 100) -> list[MemoryItem]:
        return await self.recall(namespace, "", limit)


def _row_to_item(row: object) -> MemoryItem:
    return MemoryItem(
        namespace=str(row["namespace"]),  # type: ignore[index]
        key=str(row["key"]),  # type: ignore[index]
        value=str(row["value"]),  # type: ignore[index]
        created_at=str(row["created_at"]),  # type: ignore[index]
        updated_at=str(row["updated_at"]),  # type: ignore[index]
    )
