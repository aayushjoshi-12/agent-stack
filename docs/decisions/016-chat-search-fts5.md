# ADR 016 — Chat search is SQLite FTS5 with BM25 over a disposable index

**Status:** accepted (2026-11-26). Supersedes `find_chats`'s substring scan of the newest 200
conversations, listed newest first. Amends [ADR 008](008-file-based-conversations.md).

## Context

The old search read every transcript on every query, needed every word to appear verbatim,
looked at the newest 200 chats only and did not rank. The question was whether conversations
must be stored differently (JSON files instead of the database) to search them properly. They do
not: they are already JSONL files, and scanning files is the slow part. What was missing is an
index.

## Decision

- One FTS5 table, `chat_fts`, in the existing `agent.db`: one row per message, a `said` column
  (user and agent text, compaction summaries, the title) and a `tool` column (tool calls and
  output, cut at 4k). Porter stemming. Ranked by BM25 with `tool` weighted 0.3, so a chat where a
  word was discussed beats one where it scrolled past in output.
- Query words and "quoted phrases" are quoted and joined with OR, so FTS5 syntax in a query is
  never an error and chats matching more (and rarer) words rank higher.
- The index is a copy: before a search, each chat whose transcript size/mtime or title changed is
  re-read (`chat_fts_state`). Appends, rewinds, forks and renames need no write hooks. Deleting a
  conversation deletes its rows; deleting the tables loses nothing.

## Consequences

- `agent.db` now holds message text (searchable copy), which ADR 008 had ruled out.
- Searches all of a user's chats; on real data the first search (building the index) took 6 ms
  for three chats and later ones about 1 ms.
- No semantic matching: a query that shares no words with a chat does not find it. Embeddings
  could be added on top later.
