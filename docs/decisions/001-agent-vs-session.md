# ADR 001 — Agent vs Session

**Status:** Accepted

## Context

`Agent` is tempting to overload with everything (state, memory, messages, current run).
Doing so makes the agent untestable, un-shareable across gateways, and impossible to
reason about under concurrency.

## Decision

- `Agent` is **reusable, immutable configuration**: model, tools, subagents, system
  prompt, limits.
- `Session` is the **durable conversation/work context** and survives individual
  requests.
- `Run` is **one execution** caused by one user request; a session contains many runs.
- `AgentState` is the **current mutable execution state**.

## Consequences

- No `state`, `memory`, `session`, or `event_bus` fields on `Agent`.
- Infrastructure is injected into `AgentRuntime`, not owned by `Agent`.
- Multiple gateways can share the same session because it is a domain concept, not a
  gateway concept.
