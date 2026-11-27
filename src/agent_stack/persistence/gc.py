"""Storage garbage collection (P50): what deleted chats leave behind.

The rules (``docs/design/conversation-storage.md`` section 9), conservative because deletion
cannot be undone:

- a session with a ``sessions`` row is kept, unless it is a subagent session whose chat (the
  id's first ``::`` segment) is neither a session nor a run of a kept session;
- rows of a session that has no ``sessions`` row go (with its snapshots and folder);
- a folder the index has no trace of is reported, never deleted (it may belong to another
  index: ``AGENT_STACK_DB``);
- a workspace artifact goes when no transcript and no logged event mentions its id;
- a change snapshot folder goes when no session or change row maps to it;
- anything touched within ``grace`` is kept (a run elsewhere may still be writing it).
"""

from __future__ import annotations

import asyncio
import re
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_stack.persistence.changes import LocalChangeStore, snapshot_dir
from agent_stack.persistence.conversation import ConversationStore
from agent_stack.persistence.sqlite import SESSION_TABLES, SqliteDatabase, SqliteSessionStore

__all__ = ["GRACE", "GcReport", "StorageCollector"]

GRACE = timedelta(hours=24)
_ID = re.compile(r"[0-9a-f]{32}")  # artifact ids are uuid4 hex


@dataclass
class GcReport:
    applied: bool
    sessions: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    snapshots: list[str] = field(default_factory=list)
    #: Folders with no row in the index: reported only, never deleted.
    unindexed: list[str] = field(default_factory=list)
    bytes: int = 0

    def text(self) -> str:
        head = "Deleted" if self.applied else "Would delete (dry run; --apply deletes)"
        lines = [
            f"{head}: {len(self.sessions)} orphan session(s), {len(self.artifacts)} "
            f"artifact(s), {len(self.snapshots)} change snapshot folder(s), {self.bytes} bytes."
        ]
        for label, items in (
            ("session", self.sessions),
            ("artifact", self.artifacts),
            ("snapshot", self.snapshots),
        ):
            lines += [f"  {label} {item}" for item in items]
        if self.unindexed:
            lines.append("Kept, not in the index (delete by hand if they are not another index's):")
            lines += [f"  folder {name}" for name in self.unindexed]
        return "\n".join(lines)


class StorageCollector:
    def __init__(
        self,
        db: SqliteDatabase,
        sessions: SqliteSessionStore,
        conversation: ConversationStore,
        changes: LocalChangeStore,
        *,
        sessions_root: Path,
        artifacts_root: Path,
        changes_root: Path,
    ) -> None:
        self._db = db
        self._sessions = sessions
        self._conversation = conversation
        self._changes = changes
        self._sessions_root = sessions_root
        self._artifacts_root = artifacts_root
        self._changes_root = changes_root

    async def collect(
        self, *, apply: bool = False, grace: timedelta = GRACE, now: datetime | None = None
    ) -> GcReport:
        cutoff = ((now or datetime.now(UTC)) - grace).timestamp()
        report = GcReport(applied=apply)
        known = {str(r["id"]) for r in await self._db.query("SELECT id FROM sessions")}
        runs = {
            str(r["id"]): str(r["session_id"])
            for r in await self._db.query("SELECT id, session_id FROM runs")
        }
        seen: set[str] = set()
        for table in (*SESSION_TABLES, "file_changes"):
            rows = await self._db.query(f"SELECT DISTINCT session_id FROM {table}")
            seen.update(str(r["session_id"]) for r in rows)

        alive = set(known)
        while True:  # a dead subagent's own subagents die with it
            chats = alive | {run for run, sid in runs.items() if sid in alive}
            dead = {s for s in alive if "::" in s and s.split("::", 1)[0] not in chats}
            if not dead:
                break
            alive -= dead
        for session_id in sorted((seen | known) - alive):
            if not await self._fresh(session_id, cutoff):
                report.sessions.append(session_id)
        gone = set(report.sessions)

        for path in sorted(self._sessions_root.iterdir()) if self._sessions_root.is_dir() else []:
            if path.is_dir() and path.name not in seen | known:
                report.unindexed.append(path.name)

        refs = await asyncio.to_thread(self._transcript_refs, gone)
        for row in await self._db.query("SELECT session_id, data_json FROM event_log"):
            if str(row["session_id"]) not in gone:
                refs.update(_ID.findall(str(row["data_json"])))
        artifacts = self._stale_children(self._artifacts_root, cutoff, files=True)
        artifacts = [p for p in artifacts if p.name.split(".", 1)[0] not in refs]
        report.artifacts = [p.name for p in artifacts]

        changed = await self._db.query("SELECT DISTINCT session_id FROM file_changes")
        owners = (alive | {str(r["session_id"]) for r in changed}) - gone
        # a gone session's snapshots are listed with it: its delete removes them
        wanted = {snapshot_dir(self._changes_root, s).name for s in owners | gone}
        snapshots = [
            p
            for p in self._stale_children(self._changes_root, cutoff, files=False)
            if p.name not in wanted
        ]
        report.snapshots = [p.name for p in snapshots]

        folders = [p for s in gone if (p := self._conversation.session_dir(s)).is_dir()]
        report.bytes = sum(_size(p) for p in (*artifacts, *snapshots, *folders))
        report.bytes += sum(_size(snapshot_dir(self._changes_root, s)) for s in gone)
        if apply:
            for session_id in report.sessions:
                await self._sessions.delete(session_id)
                await self._changes.delete_session(session_id)
            for path in artifacts:
                path.unlink(missing_ok=True)
            for path in snapshots:
                await asyncio.to_thread(shutil.rmtree, path, True)
        return report

    async def _fresh(self, session_id: str, cutoff: float) -> bool:
        rows = await self._db.query(
            "SELECT MAX(timestamp) AS last FROM event_log WHERE session_id = ?", (session_id,)
        )
        last = rows[0]["last"] if rows else None
        if last is not None and datetime.fromisoformat(str(last)).timestamp() >= cutoff:
            return True
        folder = self._conversation.session_dir(session_id)
        return folder.exists() and _newest(folder) >= cutoff

    def _transcript_refs(self, gone: set[str]) -> set[str]:
        skip = [self._conversation.session_dir(s) for s in gone]
        refs: set[str] = set()
        for path in self._sessions_root.rglob("transcript.jsonl"):
            if not any(path.is_relative_to(folder) for folder in skip):
                refs.update(_ID.findall(path.read_text(encoding="utf-8", errors="replace")))
        return refs

    @staticmethod
    def _stale_children(root: Path, cutoff: float, *, files: bool) -> list[Path]:
        if not root.is_dir():
            return []
        return sorted(
            p
            for p in root.iterdir()
            if (p.is_file() if files else p.is_dir()) and _newest(p) < cutoff
        )


def _newest(path: Path) -> float:
    """The latest modification time of ``path`` or anything under it."""
    stamps = [path.stat().st_mtime]
    if path.is_dir():
        stamps += [p.stat().st_mtime for p in path.rglob("*")]
    return max(stamps)


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) if path.is_dir() else 0
