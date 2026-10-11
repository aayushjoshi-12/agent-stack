# ADR 002 — Explicit loop instead of LangGraph (for now)

**Status:** Accepted

## Context

LangGraph and DeepAgents provide a graph runtime, checkpointing, and middleware. Using
them first would hide the mechanics this project exists to learn (state transitions,
persistence, cancellation, parallel tools, subagent boundaries, resume semantics).

## Decision

Implement the agent loop by hand as an explicit async model/tool loop. It stays small and
boring:

```python
while True:
    response = await model_with_tools.ainvoke(build_context(state))
    state.messages.append(response)
    if not response.tool_calls:
        return response
    state.messages.extend(await tool_executor.execute_many(response.tool_calls))
```

## Consequences

- We own persistence, checkpointing, cancellation, and resume semantics.
- Revisit LangGraph only when the custom runtime becomes harder to reason about than the
  problems it solves.
- Mapping for a future migration: `Session ≈ thread`, `CheckpointStore ≈ checkpointer`,
  `MemoryStore ≈ store`, `AgentState ≈ graph state`.
