"""File-based conversation storage.

Whole chats live as files under ``<root>/sessions/<session_id>/`` — reviewable, diffable
and greppable. SQLite is only the *index* (see :mod:`agent_stack.persistence.sqlite`); the
transcript files are the source of truth.

Layout::

    <root>/sessions/<session_id>/
    ├── session.json      # metadata mirror of the SQLite row
    ├── transcript.jsonl  # append-only, one record per line
    ├── transcript.md     # human-readable rendering, append-only
    ├── artifacts/        # files created or shared in this chat, and the chat's images
    └── subagents/<agent>-<id>/   # each subagent's own conversation, same layout, plus
                                  # subagent.json: its run ids, task, status and counts

A subagent's session id is ``<parent session id>::<agent>::<id>``, so its folder sits inside
the chat that started it (a subagent of a subagent nests one level deeper) and goes when the
chat is deleted. Older subagent folders, named by the parent *run* id, stay where they are.

An image in a message is saved once as ``artifacts/<sha256[:16]>.<ext>`` and the transcript line
names it (``data.images``); reading the conversation turns the names back into image blocks.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from langchain_core.messages import BaseMessage, HumanMessage, ToolMessage

from agent_stack.core.errors import SessionVersionConflictError
from agent_stack.core.images import (
    decode_image,
    image_block,
    image_file_name,
    placeholder,
    sniff_mime,
)
from agent_stack.core.session import Session
from agent_stack.persistence.serializers import (
    MessageRecord,
    message_to_record,
    record_from_json,
    record_images,
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

    async def image(self, session_id: str, name: str) -> bytes | None: ...

    def copy_images(self, source: str, target: str, records: Sequence[MessageRecord]) -> None: ...


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
        flat = self._sessions_root / session_id
        if "::" not in session_id or flat.is_dir():  # a chat, or an older subagent folder
            return flat
        parent, agent, child = session_id.rsplit("::", 2)
        return self.session_dir(parent) / "subagents" / f"{agent}-{child}"

    def write_subagent_record(self, session_id: str, record: dict[str, Any]) -> None:
        """Write ``subagent.json`` (run ids, task, status, counts) beside a subagent's transcript."""
        directory = self.session_dir(session_id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "subagent.json").write_text(json.dumps(record, indent=2) + "\n", "utf-8")

    async def append(self, session_id: str, records: Sequence[MessageRecord]) -> None:
        await asyncio.to_thread(self._append_sync, session_id, list(records))

    async def read(self, session_id: str) -> list[MessageRecord]:
        return await asyncio.to_thread(self._read_sync, session_id)

    async def read_messages(self, session_id: str) -> list[BaseMessage]:
        return await asyncio.to_thread(self._read_messages_sync, session_id)

    async def image(self, session_id: str, name: str) -> bytes | None:
        """A stored image of this session by its file name (None when there is none)."""
        path = self.session_dir(session_id) / "artifacts" / name
        return await asyncio.to_thread(path.read_bytes) if path.is_file() else None

    def copy_images(self, source: str, target: str, records: Sequence[MessageRecord]) -> None:
        """Copy the image files ``records`` name from one session to another (a branch)."""
        into = self.session_dir(target) / "artifacts"
        for name in {n for record in records for n in record_images(record)}:
            found = self.session_dir(source) / "artifacts" / name
            if found.is_file():
                into.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(found, into / name)

    # -- SessionStore ------------------------------------------------------

    async def get(self, session_id: str) -> Session | None:
        return await asyncio.to_thread(self._get_session_sync, session_id)

    async def save(self, session: Session, *, expected_version: int | None = None) -> None:
        await asyncio.to_thread(self._save_session_sync, session, expected_version)

    async def list_for_owner(self, owner_id: str, limit: int = 50) -> list[Session]:
        return await asyncio.to_thread(self._list_sync, owner_id, limit)

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

    def _list_sync(self, owner_id: str, limit: int) -> list[Session]:
        """Scan ``session.json`` files; recency is the file's modification time."""
        if not self._sessions_root.exists():
            return []
        found: list[tuple[float, Session]] = []
        for path in self._sessions_root.glob("*/session.json"):
            session = self._get_session_sync(path.parent.name)
            if session is not None and session.owner_id == owner_id:
                found.append((path.stat().st_mtime, session))
        found.sort(key=lambda item: item[0], reverse=True)
        return [session for _, session in found[:limit]]

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
            branch_of=payload.get("branch_of"),
            branch_point_event_id=payload.get("branch_point_event_id"),
        )

    def _save_session_sync(self, session: Session, expected_version: int | None) -> None:
        directory = self.session_dir(session.id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "artifacts").mkdir(exist_ok=True)
        if expected_version is not None:
            path = directory / "session.json"
            stored_version = 0
            if path.exists():
                raw = path.read_text(encoding="utf-8")
                stored_payload: Any = json.loads(raw)
                stored_version = int(stored_payload.get("version", 0))
            if stored_version != expected_version:
                raise SessionVersionConflictError(
                    f"session {session.id} version mismatch: expected {expected_version}, "
                    f"stored {stored_version}"
                )
        payload: dict[str, Any] = {
            "id": session.id,
            "owner_id": session.owner_id,
            "agent_name": session.agent_name,
            "title": session.title,
            "status": session.status,
            "version": session.version,
            "metadata": dict(session.metadata),
            "branch_of": session.branch_of,
            "branch_point_event_id": session.branch_point_event_id,
        }
        target = directory / "session.json"
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(target)

    def _read_messages_sync(self, session_id: str) -> list[BaseMessage]:
        artifacts = self.session_dir(session_id) / "artifacts"
        messages = []
        for record in self._read_sync(session_id):
            if record.kind == "event":
                continue
            message = record_to_message(record)
            if names := record_images(record):
                message = _with_images(message, names, artifacts)
            messages.append(message)
        return messages

    def _save_images(self, session_id: str, messages: list[BaseMessage]) -> None:
        """Write each image in ``messages`` to ``artifacts/`` once, under its content hash."""
        artifacts = self.session_dir(session_id) / "artifacts"
        for message in messages:
            for part in message.content if isinstance(message.content, list) else ():
                if (decoded := decode_image(part)) is None:
                    continue
                target = artifacts / image_file_name(*decoded)
                if not target.exists():
                    artifacts.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(decoded[0])

    def _append_messages_sync(self, session_id: str, messages: list[BaseMessage]) -> None:
        if not messages:
            return
        self._save_images(session_id, messages)
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


def _with_images(message: BaseMessage, names: list[str], artifacts: Path) -> BaseMessage:
    """``message`` with its stored images back as blocks: images first, then the text."""
    if not isinstance(message, (HumanMessage, ToolMessage)):
        return message
    blocks: list[str | dict[str, Any]] = []
    for name in names:
        try:
            data = (artifacts / name).read_bytes()
        except OSError:
            blocks.append({"type": "text", "text": placeholder(f"{name} (missing)")})
            continue
        blocks.append(image_block(data, sniff_mime(data) or "image/png"))
    if isinstance(message.content, str) and message.content:
        blocks.append({"type": "text", "text": message.content})
    return message.model_copy(update={"content": blocks})


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

    images = record_images(record)
    if images:
        lines.extend(placeholder(name) for name in images)
        lines.append("")

    data = {k: v for k, v in record.data.items() if k != "images"}
    if data:
        rendered = json.dumps(data, ensure_ascii=False, indent=2, default=str)
        lines.append(f"```json\n{rendered}\n```")
        lines.append("")

    return "\n".join(lines) + "\n"
