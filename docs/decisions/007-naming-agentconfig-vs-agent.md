# ADR 007 — Definition naming: `Agent` vs `AgentConfig`

**Status:** Proposed

## Context

The design document calls the immutable definition `Agent`. The current code calls it
`AgentConfig`, while the executor of a definition is `AgentRuntime`.

## Decision

**Recommended:** rename `AgentConfig` → `Agent` so the code speaks the domain language
(`Agent` / `AgentRuntime` / `AgentState` / `Session` / `Run`) and matches the design doc
and the ADRs. `AgentFactory.create(agent)` then reads naturally.

- Alternative: keep `AgentConfig`, but use it consistently and update the docs.

## Consequences

- A small, mechanical rename across `core/` and `app/`.
- Do this in Phase 4, before more code depends on the name.
