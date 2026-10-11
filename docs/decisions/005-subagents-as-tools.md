# ADR 005 — Subagents are tools

**Status:** Accepted

## Context

Subagents could be implemented with a dedicated scheduler, a special loop, and separate
state. That duplicates the tool execution machinery.

## Decision

Expose delegation as a normal tool:

```
delegate(agent_name, task) -> compact child result
```

The parent's normal `ToolExecutor` runs it. Internally it resolves a child `Agent` via
`AgentFactory`, runs a child run linked by `parent_run_id`, and returns a summary — not
the full child transcript.

## Consequences

- Delegation inherits concurrency, timeouts, error handling, events, and limits for free.
- Children get their own run context; only summaries/facts/artifacts flow back.
- Enforce `max_subagent_depth` and `max_subagents`.
