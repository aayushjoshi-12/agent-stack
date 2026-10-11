"""Persistence: the file-based conversation transcript plus the SQLite index.

- :mod:`agent_stack.persistence.conversation` — the full conversation as per-session files.
- :mod:`agent_stack.persistence.sqlite` — metadata, run lifecycle and index rows.
- :mod:`agent_stack.persistence.serializers` — LangChain message ⇄ JSONL records.
- :mod:`agent_stack.persistence.migrations` — schema bootstrap.
"""

from __future__ import annotations

from agent_stack.persistence.base import RunStore
from agent_stack.persistence.conversation import ConversationStore, FileConversationStore
from agent_stack.persistence.migrations import SCHEMA_VERSION, create_schema
from agent_stack.persistence.serializers import (
    MessageKind,
    MessageRecord,
    message_to_record,
    record_from_json,
    record_to_json,
    record_to_message,
)
from agent_stack.persistence.sqlite import (
    ArtifactIndexEntry,
    ConversationBackend,
    MessageIndexEntry,
    SqliteDatabase,
    SqliteIndex,
    SqliteRunStore,
    SqliteSessionStore,
)

__all__ = [
    "SCHEMA_VERSION",
    "ArtifactIndexEntry",
    "ConversationBackend",
    "ConversationStore",
    "FileConversationStore",
    "MessageIndexEntry",
    "MessageKind",
    "MessageRecord",
    "RunStore",
    "SqliteDatabase",
    "SqliteIndex",
    "SqliteRunStore",
    "SqliteSessionStore",
    "create_schema",
    "message_to_record",
    "record_from_json",
    "record_to_json",
    "record_to_message",
]
