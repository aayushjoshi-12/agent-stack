# ADR 003 — Gateways consume an event protocol

**Status:** Accepted

## Context

Gateways need to show progress, but raw model payloads and internal tool calls must not
leak to users. Different gateways also need to render the same progress differently.

## Decision

Gateways subscribe to a runtime event protocol (`AgentEvent`) and never inspect
LangChain messages directly.

```
AgentEvent = runtime/gateway contract
BaseMessage = model context
DB row = persistence representation
```

The runtime publishes `run.started`, `assistant.message`, `tool.started`,
`tool.completed`, `run.completed`, `run.failed`, later `approval.required`, etc.

## Consequences

- Never emit `response.model_dump()` to a gateway.
- Events carry monotonic sequence numbers to enable reconnect/replay.
- Adding a new gateway does not change the runtime.
- The runtime never imports a gateway.
