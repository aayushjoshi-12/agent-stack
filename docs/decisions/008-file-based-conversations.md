# ADR 008 — Conversations are files; SQLite is the index

**Status:** Accepted

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
