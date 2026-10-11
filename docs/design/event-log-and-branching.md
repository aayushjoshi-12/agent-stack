# Design — Durable event log & conversation branching

> Scheduled for **Milestone H** (Phases 29–30) — documented now, implemented after the
> gateways land. This is what turns the event bus + conversation store into a **timeline
> you can rewind and fork**.

---

## 1. Problem

Two related wants:

1. **Go back in the past.** Today events live only in the in-memory bus (P08) and the
   conversation lives in per-session files (P09). Nothing lets you *revisit an exact past
   moment* — a run's full event stream — after the process is gone, or re-render a chat as
   it looked halfway through.
2. **Branch.** A linear chat cannot explore alternatives. The user wants to fork a
   conversation at any point ("what if instead of X I had asked Y?") and continue the
   fork without touching the original — a versioned **timeline**, not a single strand.

## 2. Principles

- **Events are the timeline; messages are the payloads.** Both must be rehydratable from
  durable storage. Events carry metadata + references; message/artifact payloads live in
  the file store (ADR-008: SQLite holds no conversation content).
- **A branch is a first-class session family.** A conversation is a tree of branches;
  each branch is a linear, append-only sequence rooted at a parent event.
- **Append-only everywhere.** Rewriting history is not a feature. Branching is the
  mutation primitive; a branch never mutates its parent or siblings.

## 3. Durable event log (Phase 29)

### 3.1 Storage

```text
event_log(id, run_id, session_id, branch_id, sequence, type,
          timestamp, offset, data_json)
```

- `offset` points into `transcript.jsonl` for payload-bearing events
  (`assistant.message`, `tool.*`); `data_json` holds small structured metadata only
  (tool names, ids, step counts).
- Tables live in the same SQLite file as the index (P09), keeping one backup unit.

> **Design tension (explicit):** replay needs *some* payload. The index-is-no-content
> rule still holds — full payloads rehydrate from `transcript.jsonl` via the offset.
> Only non-secret, small metadata is inlined.

### 3.2 Interfaces

```python
class EventLogStore(Protocol):
    async def append(self, events: Sequence[AgentEvent], *, offsets: Mapping[str, int]) -> None: ...
    async def read(self, *, run_id: str | None = None, session_id: str | None = None,
                   branch_id: str | None = None, up_to_sequence: int | None = None) -> list[AgentEvent]: ...

class EventStoreBus(EventBus):      # publishes in-memory AND appends to the log
    async def replay(self, *, run_id: str | None = None, session_id: str | None = None,
                     up_to_sequence: int | None = None) -> AsyncIterator[AgentEvent]: ...
```

The P24 SSE endpoint becomes *seekable*: `Last-Event-ID` maps to a `(branch_id,
sequence)` and replay resumes from there.

## 4. Branching (Phase 30)

### 4.1 Model

```text
session X (root branch b0)
   │  seq 1..40                ← events/messages, append-only
   ▼
fork at event 25
   ├── b0 continues: seq 41, 42, …
   └── b1 (new branch): seq 1..25 copied, then 26', 27', … diverges
```

- Branch identity: `branch_id` (uuid). `Session.branch_of: str | None` +
  `Session.branch_point_event_id: str | None` on the *branch* session.
- The message index is keyed by `(session_id, branch_id, seq)`.
- File layout: `sessions/<branch_id>/` is a **copy of the parent's prefix** up to the
  fork point (`transcript.jsonl`, `.md`, `session.json`), then appended independently.
  One writer per branch (P12 session lock already gives this).
- `EventType` gains `session.branched` (data: `{parent_session_id, branch_point_event_id,
  branch_session_id}`) so the timeline shows forks.

### 4.2 Interfaces

```python
class SessionStore(Protocol):  # extends P09 protocol
    async def branch(self, session_id: str, branch_point_event_id: str,
                     *,
                     agent_name: str | None = None) -> Session: ...
```

- Runtime behavior: `run(session_id=<branch_id>, ...)` starts from the branch point, and
  a run on `b0` that predates the fork never writes into `b1` (append-only + per-session
  version bump).

## 5. What does NOT change

- The core loop (P05) — it still asks for messages, produces messages.
- Append-only conversation files (P09).
- ADR-008 (SQLite = index, no payloads) — reinforced by the offset approach.
- Gateways — they consume events; forking shows up as a new event + a new session id.

## 6. Acceptance (from the plan)

- P29: every P08 event type is in the log after a run; replay reproduces a run exactly,
  in order; events survive restart.
- P30: `test_branch_forks_at_event_and_preserves_prefix`,
  `test_branch_and_parent_diverge_after_fork`,
  `test_branch_writes_do_not_touch_parent`; UI affordance in TUI/Web (later).