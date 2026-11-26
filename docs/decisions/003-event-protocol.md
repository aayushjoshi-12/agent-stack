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

The runtime publishes `run.started`, `model.reasoning`, `assistant.message`,
`tool.started`, `tool.output`, `tool.completed`, `subagent.started`,
`subagent.completed`, `run.completed`, `run.failed`, later `approval.required`, etc.

- `model.reasoning` carries the model's thinking for a step (when the provider returns
  any), emitted just before the `assistant.message` it belongs to. It is a first-class
  event, not part of the answer, so a gateway may show or hide it.

## Consequences

- Never emit `response.model_dump()` to a gateway.
- Events carry monotonic sequence numbers to enable reconnect/replay.
- Adding a new gateway does not change the runtime.
- The runtime never imports a gateway.

## Amendment (2026-11-26): the event set as built

`core/events.py` holds the closed set. Beyond the list above: `model.usage` (tokens and cost per
model call), `approval.required` / `approval.resolved` (P26), `approval.auto` (auto mode's
verdict on a gated call), `session.branched` (P30) and `context.compacted` (P31). `tool.completed`
carries `ok` and `error`. None of these is terminal for a stream; only `run.completed` and
`run.failed` are.
