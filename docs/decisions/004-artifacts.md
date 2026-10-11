# ADR 004 — Large outputs live outside model context

**Status:** Accepted

## Context

Coding tools (especially `run_command`) can produce megabytes of output. Dumping that into
the model context is expensive and degrades reasoning.

## Decision

Cap tool output. When output exceeds the cap, store the full payload in an `ArtifactStore`
and give the model a short `ToolMessage` plus an `artifact://<id>` reference. The model
receives compact, relevant content; the full data remains retrievable.

## Consequences

- The artifact boundary is introduced early (its implementation will change: local FS →
  S3).
- Tool result summarization becomes a first-class concern.
- The model never receives embedded binary payloads.
