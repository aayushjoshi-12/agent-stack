# ADR 008 — Conversations are files; SQLite is the index

**Status:** Accepted; amended by [ADR 016](016-chat-search-fts5.md) (a search index holds a
disposable copy of message text). See "As built" below.

## Context

Storing entire conversations in SQLite as `message_json` blobs makes transcripts hard to
review, diff, grep, and export, and gives artifacts nowhere natural to live. It also grows
the database with context rather than with events.

## Decision

Store the **full conversation and its artifacts as files per session** under
`<workspace>/.agent/sessions/<session_id>/` (`session.json`, `transcript.md`,
`transcript.jsonl`, `artifacts/`). SQLite stores only the **index**: session metadata, run
lifecycle, a message index (offsets into the JSONL), and artifact metadata — never message
content.

- `transcript.jsonl` is append-only and the source of truth.
- `transcript.md` is a human-readable rendering.
- `ConversationStore` (files) and `SessionStore`/`RunStore` (SQLite) are separate
  protocols; the runtime depends on the former only through the model boundary.

See [`../design/conversation-storage.md`](../design/conversation-storage.md).

## Consequences

- Human-reviewable chats; trivial export/backup (`cp -r`).
- One writer per session (append-only + session lock, P12).
- Cross-session analytics need the index or a scan — acceptable for a personal agent.
- Supersedes any earlier plan to persist `messages` rows with payloads.

## As built (2026-11-26)

- With the SQLite-backed store (the app's), session metadata lives in the `sessions` table;
  `session.json` is written only by the file-only store used without SQLite.
- The message index (offsets into the JSONL) exists in the schema but the app never writes it.
- `chat_fts` (P32) holds a copy of message text for search. It is rebuilt from the JSONL on
  demand and removed with the conversation, so the files stay the source of truth (ADR 016).
- A saved compaction summary (P31) is one more line appended to `transcript.jsonl`; nothing is
  ever removed from it (ADR 015).
