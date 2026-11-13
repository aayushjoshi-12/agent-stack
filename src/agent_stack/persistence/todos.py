"""A session's task list in SQLite (P22).

Todos belong to the *session*, not to one run, so they survive a checkpoint resume, a later
message in the same conversation, and a process restart.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from agent_stack.core.state import TodoItem, TodoStatus
from agent_stack.persistence.sqlite import SqliteDatabase

__all__ = ["SqliteTodoStore"]


class SqliteTodoStore:
    """Add, list and complete a session's todos."""

    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db
        # Allocating an id is read-max-then-insert; serialize it so two concurrent adds in
        # one session cannot pick the same id.
        self._add_lock = asyncio.Lock()

    async def add(self, session_id: str, text: str) -> TodoItem:
        async with self._add_lock:
            rows = await self._db.query(
                "SELECT COALESCE(MAX(id), 0) + 1 AS next FROM todos WHERE session_id = ?",
                (session_id,),
            )
            todo_id = int(rows[0]["next"])
            await self._db.execute(
                "INSERT INTO todos (session_id, id, text, status, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    session_id,
                    todo_id,
                    text,
                    TodoStatus.PENDING.value,
                    datetime.now(UTC).isoformat(),
                ),
            )
        return TodoItem(id=todo_id, text=text)

    async def list_items(self, session_id: str) -> list[TodoItem]:
        rows = await self._db.query(
            "SELECT id, text, status FROM todos WHERE session_id = ? ORDER BY id", (session_id,)
        )
        return [TodoItem(int(r["id"]), str(r["text"]), TodoStatus(str(r["status"]))) for r in rows]

    async def complete(self, session_id: str, todo_id: int) -> bool:
        """Mark a todo done; ``False`` when it does not exist."""
        changed = await self._db.execute_rowcount(
            "UPDATE todos SET status = ? WHERE session_id = ? AND id = ?",
            (TodoStatus.DONE.value, session_id, todo_id),
        )
        return changed > 0
