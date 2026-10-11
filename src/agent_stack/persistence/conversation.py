"""File-based conversation storage.

Whole chats live as files under ``<root>/sessions/<session_id>/`` — reviewable, diffable
and greppable. SQLite is only the *index* (see :mod:`agent_stack.persistence.sqlite`); the
transcript files are the source of truth.

Layout::

    <root>/sessions/<session_id>/
    ├── session.json      # metadata mirror of the SQLite row
    ├── transcript.jsonl  # append-only, one record per line
    ├── transcript.md     # human-readable rendering, append-only
    └── artifacts/        # files created or shared in this chat
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from langchain_core.messages import BaseMessage

from agent_stack.core.session import Session
from agent_stack.persistence.serializers import (
    MessageRecord,
    message_to_record,
    record_from_json,
    record_to_json,
    record_to_message,
)


@runtime_checkable
class ConversationStore(Protocol):
    """Infrastructure boundary for the full conversation + artifacts."""

    async def append(self, session_id: str, records: Sequence[MessageRecord]) -> None: ...

    async def read(self, session_id: str) -> list[MessageRecord]: ...

    async def read_messages(self, session_id: str) -> list[BaseMessage]: ...

    def session_dir(self, session_id: str) -> Path: ...


class FileConversationStore:
    """A :class:`ConversationStore` backed by per-session files.

    Also satisfies :class:`agent_stack.core.session.SessionStore` end to end, so it can be
    dropped in wherever the runtime expects a store: metadata is mirrored in ``session.json``
    and message appends land in ``transcript.jsonl`` + ``transcript.md`` with monotonic
    ``seq``.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._sessions_root = self._root / "sessions"

    # -- ConversationStore -------------------------------------------------

    def session_dir(self, session_id: str) -> Path:
        return self._sessions_root / session_id

    async def append(self, session_id: str, records: Sequence[MessageRecord]) -> None:
        await asyncio.to_thread(self._append_sync, session_id, list(records))

    async def read(self, session_id: str) -> list[MessageRecord]:
        return await asyncio.to_thread(self._read_sync, session_id)

    async def read_messages(self, session_id: str) -> list[BaseMessage]:
        records = await self.read(session_id)
        return [record_to_message(record) for record in records if record.kind != "event"]

    # -- SessionStore ------------------------------------------------------

    async def get(self, session_id: str) -> Session | None:
        return await asyncio.to_thread(self._get_session_sync, session_id)

    async def save(self, session: Session) -> None:
        await asyncio.to_thread(self._save_session_sync, session)

    async def get_messages(self, session_id: str) -> list[BaseMessage]:
        return await self.read_messages(session_id)

    async def append_messages(self, session_id: str, messages: Sequence[BaseMessage]) -> None:
        await asyncio.to_thread(self._append_messages_sync, session_id, list(messages))

    async def delete(self, session_id: str) -> None:
        await asyncio.to_thread(self._delete_sync, session_id)

    # -- synchronous internals --------------------------------------------

    def _append_sync(self, session_id: str, records: list[MessageRecord]) -> None:
        if not records:
            return
        directory = self.session_dir(session_id)
        (directory / "artifacts").mkdir(parents=True, exist_ok=True)
        with (directory / "transcript.jsonl").open("a", encoding="utf-8") as jsonl:
            for record in records:
                jsonl.write(record_to_json(record))
                jsonl.write("\n")
        with (directory / "transcript.md").open("a", encoding="utf-8") as markdown:
            for record in records:
                markdown.write(_render_record(record))

    def _read_sync(self, session_id: str) -> list[MessageRecord]:
        path = self.session_dir(session_id) / "transcript.jsonl"
        if not path.exists():
            return []
        records: list[MessageRecord] = []
        with path.open("r", encoding="utf-8") as jsonl:
            for line in jsonl:
                stripped = line.strip()
                if stripped:
                    records.append(record_from_json(stripped))
        return records

    def _get_session_sync(self, session_id: str) -> Session | None:
        path = self.session_dir(session_id) / "session.json"
        if not path.exists():
            return None
        raw = path.read_text(encoding="utf-8")
        payload: Any = json.loads(raw)
        metadata = payload.get("metadata")
        return Session(
            id=str(payload["id"]),
            owner_id=str(payload["owner_id"]),
            agent_name=str(payload["agent_name"]),
            title=None if payload.get("title") is None else str(payload["title"]),
            status=str(payload.get("status", "active")),
            version=int(payload.get("version", 0)),
            metadata=dict(metadata) if isinstance(metadata, dict) else {},
        )

    def _save_session_sync(self, session: Session) -> None:
        directory = self.session_dir(session.id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "artifacts").mkdir(exist_ok=True)
        payload: dict[str, Any] = {
            "id": session.id,
            "owner_id": session.owner_id,
            "agent_name": session.agent_name,
            "title": session.title,
            "status": session.status,
            "version": session.version,
            "metadata": dict(session.metadata),
        }
        target = directory / "session.json"
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(target)

    def _append_messages_sync(self, session_id: str, messages: list[BaseMessage]) -> None:
        if not messages:
            return
        existing = self._read_sync(session_id)
        start = max((record.seq for record in existing), default=0) + 1
        now = datetime.now(UTC)
        records = [
            message_to_record(message, seq=start + offset, run_id="", ts=now)
            for offset, message in enumerate(messages)
        ]
        self._append_sync(session_id, records)

    def _delete_sync(self, session_id: str) -> None:
        shutil.rmtree(self.session_dir(session_id), ignore_errors=True)


def _render_record(record: MessageRecord) -> str:
    """Render one record as a readable Markdown section."""
    lines = [f"## {record.seq} · {record.kind}", ""]
    meta = [f"`{record.ts.isoformat()}`"]
    if record.run_id:
        meta.append(f"run `{record.run_id}`")
    lines.append(" · ".join(meta))
    lines.append("")

    for call in record.tool_calls:
        name = call.get("name", "?")
        args = json.dumps(call.get("args", {}), ensure_ascii=False, sort_keys=True)
        call_id = call.get("id")
        suffix = f" (id `{call_id}`)" if call_id else ""
        lines.append(f"- **tool call** `{name}` {args}{suffix}")
    if record.tool_calls:
        lines.append("")

    if record.tool_call_id:
        lines.append(f"- _tool_call_id_: `{record.tool_call_id}`")
        lines.append("")

    if record.content:
        lines.append(record.content)
        lines.append("")

    if record.data:
        rendered = json.dumps(dict(record.data), ensure_ascii=False, indent=2, default=str)
        lines.append(f"```json\n{rendered}\n```")
        lines.append("")

    return "\n".join(lines) + "\n"
