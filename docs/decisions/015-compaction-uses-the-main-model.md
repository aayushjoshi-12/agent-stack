# ADR 015 — Compaction is written by the conversation's own model

**Status:** accepted (2026-11-26). Supersedes the first P31 cut (same day), which summarised with
`AGENT_STACK_SMALL_MODEL` and fell back to the main model, and the earlier in-memory summary of
dropped messages (small model only, lost after each run).

## Context

A compaction summary replaces the whole conversation: every later step works from it alone, so a
dropped constraint or a wrong path is lost for good. It runs about once per 100k tokens of
growth, while every ordinary call already sends that much context to the main model, so a
cheaper summariser saves little. Claude Code and opencode both summarise with the conversation's
own model and send it the real conversation with the request appended. The first cut here also
re-rendered the transcript as text with each tool result cut to 1,200 characters, which lost more
than the choice of model did.

## Decision

- Past `AGENT_STACK_COMPACT_AT` (default 0.8) of `AGENT_STACK_CONTEXT_BUDGET`, the runtime sends
  its own model exactly the input it was about to send, plus `COMPACT_PROMPT` at the end. It
  follows `/model`. The call's usage is recorded like any other.
- The summary is appended to the transcript as a marker; the model sees only the latest marker
  and what follows. Nothing is deleted, so the UIs, rewind and forks keep every message.
- Up to five files the summarised messages read or edited (inside the workspace) are attached
  as they are at that moment, as Claude Code does.
- One failed compaction turns it off for that run; clearing old tool results and dropping old
  messages remain the fallback.

## Consequences

- A compaction costs one main-model call over at most the budget; prompt caches can reuse it.
- A conversation whose bounded window has already dropped its start loses that start from the
  summary too (chunked summaries are deferred).
- Not yet run against a real model (only the scripted model).
