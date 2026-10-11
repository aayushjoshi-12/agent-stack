"""SQLite schema bootstrap.

SQLite is the *index*, never the payload: there is deliberately no message-content or
artifact-bytes column anywhere. The full conversation lives in the per-session transcript
files (see :mod:`agent_stack.persistence.conversation`).
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    owner_id      TEXT NOT NULL,
    agent_name    TEXT NOT NULL,
    title         TEXT,
    status        TEXT NOT NULL DEFAULT 'active',
    version       INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS runs (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    parent_run_id   TEXT,
    status          TEXT NOT NULL,
    step_count      INTEGER NOT NULL DEFAULT 0,
    tool_call_count INTEGER NOT NULL DEFAULT 0,
    started_at      TEXT,
    finished_at     TEXT,
    error           TEXT,
    metadata_json   TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS message_index (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    run_id       TEXT,
    seq          INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    jsonl_offset INTEGER NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts_index (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    run_id     TEXT,
    name       TEXT NOT NULL,
    rel_path   TEXT NOT NULL,
    mime_type  TEXT,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_session ON runs(session_id);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE INDEX IF NOT EXISTS idx_message_index_session ON message_index(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_artifacts_index_session ON artifacts_index(session_id);
"""


def create_schema(conn: sqlite3.Connection) -> None:
    """Create every table/index if it does not already exist and commit."""
    conn.executescript(_SCHEMA)
    conn.commit()
