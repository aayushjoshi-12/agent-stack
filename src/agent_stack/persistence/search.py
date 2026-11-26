"""Full-text search over every conversation (P32): SQLite FTS5, ranked with BM25.

The transcripts stay the source of truth; this index is a disposable copy of their text. It is
kept in sync lazily: before a search, each conversation whose transcript changed since it was
indexed (its size, modification time or title differ) is read again and re-indexed, so appends,
rewinds, forks and renames are all picked up without hooking every write path. Deleting the
index tables loses nothing; the next search rebuilds them.

One row per message, two text columns: ``said`` (what the user and the agent wrote, compaction
summaries and the title) and ``tool`` (tool calls and their output). BM25 weighs ``tool`` lower,
so a chat where a word was *discussed* outranks one where it only scrolled past in output.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

from agent_stack.persistence.conversation import FileConversationStore
from agent_stack.persistence.serializers import MessageRecord
from agent_stack.persistence.sqlite import SqliteDatabase

__all__ = ["ChatSearchIndex", "SearchHit"]

#: Tool output is cut to this before indexing; the start of it carries most of the meaning.
_TOOL_CHARS = 4000
#: Rows fetched per session wanted: enough to group several hits per conversation.
_ROWS_PER_HIT = 20
#: BM25 weights per column: session_id, seq (unindexed), said, tool.
_RANK = "bm25(chat_fts, 0.0, 0.0, 1.0, 0.3)"


@dataclass(frozen=True)
class SearchHit:
    session_id: str
    #: The best-matching passages, best first, with the matched words in [brackets].
    snippets: tuple[str, ...]
    #: Transcript line of the best match (-1: the title).
    seq: int


def match_query(query: str) -> str:
    """The user's words as an FTS5 query: each word or "quoted phrase" quoted, joined by OR.

    Quoting keeps FTS5 syntax (AND, NEAR, ``*``, ``:``) in a query from being read as operators;
    OR lets BM25 rank chats that match more of the words higher instead of hiding the rest.
    """
    terms = [t.strip('"') for t in re.findall(r'"[^"]+"|\w+', query)]
    return " OR ".join('"' + t.replace('"', "") + '"' for t in terms if t.strip())


def _row(record: MessageRecord) -> tuple[str, str] | None:
    """``(said, tool)`` text for one transcript line, or None for lines not worth indexing."""
    if record.kind in ("human", "system"):
        return (record.content, "") if record.content.strip() else None
    if record.kind == "ai":
        calls = " ".join(
            f"{call.get('name')} {json.dumps(call.get('args'), ensure_ascii=False)}"
            for call in record.tool_calls
        )
        return (record.content, calls) if record.content.strip() or calls else None
    if record.kind == "tool":
        return ("", record.content[:_TOOL_CHARS]) if record.content.strip() else None
    return None  # events


class ChatSearchIndex:
    """Ranked full-text search over the transcripts of a :class:`FileConversationStore`."""

    def __init__(self, db: SqliteDatabase, transcripts: FileConversationStore) -> None:
        self._db = db
        self._transcripts = transcripts

    def _stamp(self, session_id: str, title: str) -> str:
        path = self._transcripts.session_dir(session_id) / "transcript.jsonl"
        try:
            stat = path.stat()
        except OSError:
            return ""
        return f"{stat.st_size}:{stat.st_mtime_ns}:{title}"

    async def sync(self, sessions: Sequence[tuple[str, str]]) -> None:
        """Re-index each ``(session_id, title)`` whose transcript or title changed."""
        known = {
            row["session_id"]: row["stamp"]
            for row in await self._db.query("SELECT session_id, stamp FROM chat_fts_state")
        }
        for session_id, title in sessions:
            stamp = self._stamp(session_id, title)
            if known.get(session_id) == stamp:
                continue
            # state first: a crash half-way leaves the chat marked stale, so it is redone
            await self._db.execute("DELETE FROM chat_fts_state WHERE session_id = ?", (session_id,))
            await self._db.execute("DELETE FROM chat_fts WHERE session_id = ?", (session_id,))
            rows: list[tuple[str, int, str, str]] = [(session_id, -1, title, "")] if title else []
            for record in await self._transcripts.read(session_id):
                text = _row(record)
                if text is not None:
                    rows.append((session_id, record.seq, *text))
            await self._db.executemany(
                "INSERT INTO chat_fts (session_id, seq, said, tool) VALUES (?, ?, ?, ?)", rows
            )
            await self._db.execute(
                "INSERT INTO chat_fts_state (session_id, stamp) VALUES (?, ?)", (session_id, stamp)
            )

    async def search(
        self, sessions: Sequence[tuple[str, str]], query: str, limit: int = 6
    ) -> list[SearchHit]:
        """The conversations among ``sessions`` that best match ``query``, best first."""
        match = match_query(query)
        if not match or not sessions:
            return []
        await self.sync(sessions)
        ids = [session_id for session_id, _ in sessions]
        rows = await self._db.query(
            "SELECT session_id, seq, snippet(chat_fts, -1, '[', ']', '…', 16) AS snip "
            f"FROM chat_fts WHERE chat_fts MATCH ? AND session_id IN ({','.join('?' * len(ids))}) "
            f"ORDER BY {_RANK} LIMIT ?",
            (match, *ids, limit * _ROWS_PER_HIT),
        )
        hits: dict[str, SearchHit] = {}
        for row in rows:  # best first, so a chat's first row is its best match
            found = hits.get(row["session_id"])
            if found is None:
                if len(hits) < limit:
                    hits[row["session_id"]] = SearchHit(
                        row["session_id"], (row["snip"],), row["seq"]
                    )
            elif len(found.snippets) < 3 and row["snip"] not in found.snippets:
                hits[row["session_id"]] = SearchHit(
                    found.session_id, (*found.snippets, row["snip"]), found.seq
                )
        return list(hits.values())
