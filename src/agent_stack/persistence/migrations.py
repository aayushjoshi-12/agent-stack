"""SQLite schema bootstrap.

SQLite is the *index*, never the payload: there is deliberately no message-content or
artifact-bytes column anywhere. The full conversation lives in the per-session transcript
files (see :mod:`agent_stack.persistence.conversation`).
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 6

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
    metadata_json TEXT NOT NULL DEFAULT '{}',
    branch_of     TEXT,
    branch_point_event_id TEXT
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

CREATE TABLE IF NOT EXISTS checkpoints (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           TEXT NOT NULL,
    session_id       TEXT NOT NULL,
    step             INTEGER NOT NULL,
    tool_call_count  INTEGER NOT NULL,
    status           TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_executions (
    run_id       TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    tool_name    TEXT NOT NULL,
    status       TEXT NOT NULL,
    error        TEXT,
    ts           TEXT NOT NULL,
    PRIMARY KEY (run_id, tool_call_id)
);

CREATE TABLE IF NOT EXISTS memory_items (
    namespace  TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (namespace, key)
);

CREATE TABLE IF NOT EXISTS todos (
    session_id TEXT NOT NULL,
    id         INTEGER NOT NULL,
    text       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    PRIMARY KEY (session_id, id)
);

CREATE TABLE IF NOT EXISTS event_log (
    id            TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL,
    session_id    TEXT NOT NULL,
    parent_run_id TEXT,
    sequence      INTEGER NOT NULL,
    type          TEXT NOT NULL,
    timestamp     TEXT NOT NULL,
    data_json     TEXT NOT NULL
);

-- Index of files the agent wrote or edited. The text before/after lives in files under
-- `.agent/changes/` (ADR-008: SQLite is an index, never the content store).
CREATE TABLE IF NOT EXISTS file_changes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    run_id     TEXT NOT NULL,
    path       TEXT NOT NULL,
    before_ref TEXT,
    after_ref  TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_file_changes_session ON file_changes(session_id, id);
CREATE INDEX IF NOT EXISTS idx_file_changes_run ON file_changes(run_id);
CREATE INDEX IF NOT EXISTS idx_event_log_run ON event_log(run_id, sequence);
CREATE INDEX IF NOT EXISTS idx_event_log_session ON event_log(session_id);
CREATE INDEX IF NOT EXISTS idx_checkpoints_run ON checkpoints(run_id);
CREATE INDEX IF NOT EXISTS idx_tool_executions_run ON tool_executions(run_id);
"""


def create_schema(conn: sqlite3.Connection) -> None:
    """Create every table/index if it does not already exist and commit."""
    conn.executescript(_SCHEMA)
    # Databases created before P30 lack the branch columns on `sessions`.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    for name in ("branch_of", "branch_point_event_id"):
        if name not in columns:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} TEXT")
    conn.commit()
