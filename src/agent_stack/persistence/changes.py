"""Change store: SQLite indexes each file edit; the before/after text lives in files.

``<root>/<session>/<uuid>.before|after`` hold the text (UTF-8); the ``file_changes`` row holds
only ids and paths, as ADR-008 requires of every SQLite table.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from agent_stack.core.changes import ChangeEntry, FileDiff, make_diff

if TYPE_CHECKING:
    from agent_stack.persistence.sqlite import SqliteDatabase

__all__ = ["LocalChangeStore"]

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def _session_dir(root: Path, session_id: str) -> Path:
    """A directory name for a session id (ids contain ``:``), unique even after sanitising."""
    digest = hashlib.sha1(session_id.encode(), usedforsecurity=False).hexdigest()[:8]
    return root / f"{_UNSAFE.sub('_', session_id)[:80]}-{digest}"


class LocalChangeStore:
    """``ChangeStore`` backed by the index database and a directory of snapshots."""

    def __init__(self, db: SqliteDatabase, root: Path) -> None:
        self._db = db
        self._root = Path(root)

    async def folder_state(self) -> str | None:
        marker = self._root / ".folder-state"
        try:
            return await asyncio.to_thread(marker.read_text, encoding="utf-8") or None
        except OSError:
            return None

    async def set_folder_state(self, session_id: str) -> None:
        def write() -> None:
            self._root.mkdir(parents=True, exist_ok=True)
            (self._root / ".folder-state").write_text(session_id, encoding="utf-8")

        await asyncio.to_thread(write)

    async def record(
        self, *, session_id: str, run_id: str, path: str, before: str | None, after: str
    ) -> None:
        await self.set_folder_state(session_id)  # the folder now holds this conversation's edit
        await self._record_at(
            session_id, run_id, path, before, after, datetime.now(UTC).isoformat()
        )

    async def _record_at(
        self,
        session_id: str,
        run_id: str,
        path: str,
        before: str | None,
        after: str,
        created_at: str,
    ) -> None:
        directory = _session_dir(self._root, session_id)
        name = uuid.uuid4().hex
        after_ref = f"{name}.after"
        before_ref = f"{name}.before" if before is not None else None

        def write() -> None:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / after_ref).write_text(after, encoding="utf-8")
            if before_ref is not None and before is not None:
                (directory / before_ref).write_text(before, encoding="utf-8")

        await asyncio.to_thread(write)  # snapshots first: a row never points at a missing file
        await self._db.execute(
            "INSERT INTO file_changes (session_id, run_id, path, before_ref, after_ref, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, run_id, path, before_ref, after_ref, created_at),
        )

    @staticmethod
    def _scope(session_id: str, run_id: str | None) -> tuple[str, str, list[str]]:
        """SQL for "this conversation (or run) and every subagent run under it"."""
        if run_id is not None:
            seed, seed_params = "SELECT ?", [run_id]
            where, where_params = "run_id IN (SELECT run_id FROM runs)", []
        else:
            seed, seed_params = (
                "SELECT DISTINCT run_id FROM event_log WHERE session_id = ?",
                [session_id],
            )
            where, where_params = (
                "(session_id = ? OR run_id IN (SELECT run_id FROM runs))",
                [session_id],
            )
        cte = (
            f"WITH RECURSIVE runs(run_id) AS ({seed} UNION "
            "SELECT DISTINCT e.run_id FROM event_log e JOIN runs r ON e.parent_run_id = r.run_id) "
        )
        return cte, where, [*seed_params, *where_params]

    async def paths(self, session_id: str, run_id: str | None = None) -> list[str]:
        cte, where, params = self._scope(session_id, run_id)
        rows = await self._db.query(
            f"{cte}SELECT path FROM file_changes WHERE {where} GROUP BY path ORDER BY MIN(id)",
            params,
        )
        return [str(row["path"]) for row in rows]

    async def file_diff(
        self, session_id: str, path: str, run_id: str | None = None, *, last_only: bool = False
    ) -> FileDiff | None:
        cte, where, params = self._scope(session_id, run_id)
        rows = await self._db.query(
            f"{cte}SELECT session_id, before_ref, after_ref FROM file_changes "
            f"WHERE {where} AND path = ? ORDER BY id",
            [*params, path],
        )
        if not rows:
            return None
        first, last = rows[-1] if last_only else rows[0], rows[-1]
        rows = rows[-1:] if last_only else rows

        def read() -> tuple[str | None, str] | None:
            try:
                before = (
                    None
                    if first["before_ref"] is None
                    else (
                        _session_dir(self._root, str(first["session_id"])) / first["before_ref"]
                    ).read_text(encoding="utf-8")
                )
                after = (
                    _session_dir(self._root, str(last["session_id"])) / last["after_ref"]
                ).read_text(encoding="utf-8")
            except OSError:
                return None  # a snapshot was removed by hand
            return before, after

        texts = await asyncio.to_thread(read)
        if texts is None:
            return None
        before, after = texts
        if before == after:
            diff, added, removed, truncated = "", 0, 0, False
        else:
            diff, added, removed, truncated = make_diff(path, before, after)
        return FileDiff(
            path=path,
            kind="created" if before is None else "modified",
            edits=len(rows),
            added=added,
            removed=removed,
            diff=diff,
            before=before,
            after=after,
            diff_truncated=truncated,
        )

    async def entries(self, session_id: str) -> list[ChangeEntry]:
        cte, where, params = self._scope(session_id, None)
        rows = await self._db.query(
            f"{cte}SELECT id, session_id, run_id, path, before_ref, after_ref, created_at "
            f"FROM file_changes WHERE {where} ORDER BY id",
            params,
        )

        def read() -> list[ChangeEntry]:
            found: list[ChangeEntry] = []
            for row in rows:
                directory = _session_dir(self._root, str(row["session_id"]))
                try:
                    before = (
                        None
                        if row["before_ref"] is None
                        else (directory / row["before_ref"]).read_text(encoding="utf-8")
                    )
                    after = (directory / row["after_ref"]).read_text(encoding="utf-8")
                except OSError:
                    continue  # a snapshot was removed by hand: that edit cannot be replayed
                found.append(
                    ChangeEntry(
                        id=int(row["id"]),
                        session_id=str(row["session_id"]),
                        run_id=str(row["run_id"]),
                        path=str(row["path"]),
                        before=before,
                        after=after,
                        created_at=str(row["created_at"]),
                    )
                )
            return found

        return await asyncio.to_thread(read)

    async def copy_upto(self, source: str, target: str, upto: str) -> int:
        from datetime import datetime

        limit = datetime.fromisoformat(upto)
        copied = 0
        for entry in await self.entries(source):
            if datetime.fromisoformat(entry.created_at) > limit:
                continue
            # a distinct run id: the source's change scope is "its runs", and these copies must
            # not be counted as the source's own edits
            await self._record_at(
                target,
                f"fork:{target}:{entry.run_id}",
                entry.path,
                entry.before,
                entry.after,
                entry.created_at,
            )
            copied += 1
        return copied

    async def delete_session(self, session_id: str) -> None:
        await self._db.execute("DELETE FROM file_changes WHERE session_id = ?", (session_id,))
        directory = _session_dir(self._root, session_id)
        await asyncio.to_thread(shutil.rmtree, directory, True)
