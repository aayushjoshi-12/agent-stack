# ADR 006 — One composition root

**Status:** Accepted

## Context

If each class constructs its own dependencies, the object graph becomes implicit,
untestable, and impossible to swap (e.g. in-memory vs SQLite). The missing concept in the
original design was **composition**: who creates objects and passes what into whom.

## Decision

`app/bootstrap.py` exposes a single `create_application(config)` that constructs the
object graph from the leaves toward the root:

```
Tools → ToolRegistry → ToolExecutor
Model
Stores
      → AgentRuntime → RunManager → Application → Gateways
```

No dependency-injection framework. Plain constructor injection only.

## Consequences

- Swapping infrastructure (in-memory → SQLite, local → S3) changes only bootstrap.
- Every class receives exactly the dependencies it needs (no god objects).
- `AgentFactory` centralizes runtime construction so subagents reuse it.
