"""LangChain message ⇄ normalized JSON records.

The conversation transcript is stored as one JSON object per line (JSONL). A
:class:`MessageRecord` is the normalized, storage-level shape of a single turn. The
conversion to and from LangChain messages happens *here*, at the model boundary, so the
rest of the domain never sees raw rows (ADR 001/003).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, cast

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.tool import ToolCall

from agent_stack.core.errors import PersistenceError
from agent_stack.core.images import block_name, placeholder

MessageKind = Literal["human", "ai", "tool", "system", "event"]

_MESSAGE_KINDS: frozenset[str] = frozenset({"human", "ai", "tool", "system", "event"})


@dataclass(frozen=True)
class MessageRecord:
    """One normalized line of the transcript.

    ``seq`` is monotonic within a session; ``ts`` is timezone-aware. ``tool_calls`` and
    ``tool_call_id`` preserve the exact LangChain tool plumbing so a round trip is lossless.
    A message's images are not inline: ``data["images"]`` names their files in the session's
    ``artifacts/`` (P44), and ``content`` is the text alone.
    """

    seq: int
    ts: datetime
    run_id: str
    kind: MessageKind
    content: str = ""
    tool_calls: tuple[dict[str, Any], ...] = ()
    tool_call_id: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)


def message_to_record(
    msg: BaseMessage,
    *,
    seq: int,
    run_id: str,
    ts: datetime,
) -> MessageRecord:
    """Convert a LangChain message into a :class:`MessageRecord`."""
    if isinstance(msg, HumanMessage):
        return MessageRecord(
            seq=seq,
            ts=ts,
            run_id=run_id,
            kind="human",
            content=_content_to_text(msg.content),
            data=_image_refs(msg.content),
        )
    if isinstance(msg, AIMessage):
        return MessageRecord(
            seq=seq,
            ts=ts,
            run_id=run_id,
            kind="ai",
            content=_content_to_text(msg.content),
            tool_calls=_normalize_tool_calls(msg.tool_calls),
        )
    if isinstance(msg, ToolMessage):
        return MessageRecord(
            seq=seq,
            ts=ts,
            run_id=run_id,
            kind="tool",
            content=_content_to_text(msg.content),
            tool_call_id=msg.tool_call_id,
            data=_image_refs(msg.content),
        )
    if isinstance(msg, SystemMessage):
        return MessageRecord(
            seq=seq,
            ts=ts,
            run_id=run_id,
            kind="system",
            content=_content_to_text(msg.content),
        )
    raise PersistenceError(f"Cannot serialize message of type {type(msg).__name__}")


def record_to_message(record: MessageRecord) -> BaseMessage:
    """Rebuild a LangChain message from a :class:`MessageRecord`.

    ``event`` records are not messages; converting one is a programming error.
    """
    if record.kind == "human":
        return HumanMessage(content=record.content)
    if record.kind == "ai":
        tool_calls = cast("list[ToolCall]", [dict(tc) for tc in record.tool_calls])
        return AIMessage(content=record.content, tool_calls=tool_calls)
    if record.kind == "tool":
        return ToolMessage(content=record.content, tool_call_id=record.tool_call_id or "")
    if record.kind == "system":
        return SystemMessage(content=record.content)
    raise PersistenceError(f"Record kind {record.kind!r} is not a message")


def record_images(record: MessageRecord) -> list[str]:
    """The stored file names of a record's images (oldest transcripts have none)."""
    names = record.data.get("images")
    return [str(n) for n in names] if isinstance(names, list) else []


def record_text(record: MessageRecord) -> str:
    """A record's text with a ``[image: name]`` line per image: what people and indexes read."""
    marks = [placeholder(name) for name in record_images(record)]
    return "\n".join([record.content, *marks] if record.content else marks)


def _image_refs(content: object) -> dict[str, Any]:
    if not isinstance(content, list):
        return {}
    images = [name for part in content if (name := block_name(part))]
    return {"images": images} if images else {}


def record_to_json(record: MessageRecord) -> str:
    """Serialize a record to a single JSONL line (no trailing newline)."""
    payload: dict[str, Any] = {
        "seq": record.seq,
        "ts": record.ts.isoformat(),
        "run_id": record.run_id,
        "kind": record.kind,
        "content": record.content,
        "tool_calls": [dict(tc) for tc in record.tool_calls],
        "tool_call_id": record.tool_call_id,
        "data": dict(record.data),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def record_from_json(line: str) -> MessageRecord:
    """Parse a single JSONL line back into a :class:`MessageRecord`."""
    try:
        raw: Any = json.loads(line)
    except json.JSONDecodeError as exc:
        raise PersistenceError(f"Malformed transcript line: {line!r}") from exc
    if not isinstance(raw, dict):
        raise PersistenceError(f"Transcript line is not a JSON object: {line!r}")

    try:
        seq = int(raw["seq"])
        ts = _parse_ts(raw["ts"])
        run_id = str(raw["run_id"])
        kind_value = str(raw["kind"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PersistenceError(f"Malformed transcript record: {line!r}") from exc

    if kind_value not in _MESSAGE_KINDS:
        raise PersistenceError(f"Unknown transcript kind {kind_value!r}")
    kind = cast(MessageKind, kind_value)

    tool_call_id_value = raw.get("tool_call_id")
    data_value = raw.get("data")

    return MessageRecord(
        seq=seq,
        ts=ts,
        run_id=run_id,
        kind=kind,
        content=str(raw.get("content", "")),
        tool_calls=tuple(_as_dict(tc) for tc in (raw.get("tool_calls") or [])),
        tool_call_id=None if tool_call_id_value is None else str(tool_call_id_value),
        data=dict(data_value) if isinstance(data_value, dict) else {},
    )


def _content_to_text(content: object) -> str:
    """Flatten a message ``content`` (string or content-block list) into text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
                elif part.get("type") == "image_url":
                    continue  # stored as a file, named in the record's data (P44)
                else:
                    parts.append(json.dumps(part, ensure_ascii=False, default=str))
            else:
                parts.append(str(part))
        return "".join(parts)
    return str(content)


def _normalize_tool_calls(tool_calls: list[ToolCall]) -> tuple[dict[str, Any], ...]:
    """Copy tool calls into plain, JSON-serializable dicts."""
    normalized: list[dict[str, Any]] = []
    for call in tool_calls:
        raw = dict(call)
        args = raw.get("args")
        normalized.append(
            {
                "id": raw.get("id"),
                "name": raw.get("name"),
                "args": dict(args) if isinstance(args, Mapping) else {},
                "type": raw.get("type", "tool_call"),
            }
        )
    return tuple(normalized)


def _as_dict(value: Any) -> dict[str, Any]:
    """Return ``value`` as a ``dict``, or an empty dict when it is not a mapping."""
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise PersistenceError(f"Invalid timestamp value: {value!r}")
