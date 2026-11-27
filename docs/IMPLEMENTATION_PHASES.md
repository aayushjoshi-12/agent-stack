# agent-stack — Phased Implementation Plan

> A single, ordered roadmap that turns the architecture design document **and** the
> composition-root guidance into a sequence of small, buildable, testable phases.

| | |
|---|---|
| **Package** | `agent_stack` (`src/agent_stack/`) |
| **Python** | 3.12+ · `uv` |
| **Style** | async-first, single process, SQLite first |
| **Created** | 2026-10-11 |
| **Companion docs** | [`design/source-design-document.md`](design/source-design-document.md) · [`design/file-capabilities.md`](design/file-capabilities.md) · [`decisions/`](decisions/) |

---

## 0. How to read this plan

1. **One phase = one concept = one commit.** Do not start a phase until the previous
   phase's acceptance checks pass.
2. Every phase has the same shape:
   **Goal · Why it exists · Deliverables · Key interfaces · Acceptance · Defer.**
3. Status markers used throughout:

   | Marker | Meaning |
   |---|---|
   | ✅ | done and verified |
   | 🟡 | partially done — needs finishing/hardening |
   | ⬜ | not started |

4. Phases are grouped into **milestones**. A milestone is a demoable capability, not a
   unit of work.
5. The **Defer** list is a promise to your future self. If a phase tempts you to build
   something on the "Do Not Build Yet" list (§Appendix D), write it down and move on.

> **The one rule that keeps this project simple:**
> *Build from the leaves toward the root, and only give each object the dependencies it
> actually needs.* See §2.

---

## 1. Non-negotiable principles

These five ideas are the difference between "an agent runtime I understand" and
"a framework I can't debug."

### 1.1 There is exactly one composition root

`app/bootstrap.py` is the **only** place that constructs the object graph. Classes never
build their own dependencies.

```python
# Good: objects declare needs, bootstrap satisfies them.
runtime = AgentRuntime(model=model, tool_executor=executor, session_store=store)

# Bad: a class constructing its own world (untestable, un-swappable).
class AgentRuntime:
    def __init__(self):
        self.model = init_chat_model(...)
        self.db = sqlite3.connect(...)
```

### 1.2 Dependency direction is one-way and downward

```text
Gateway  →  Application  →  RunManager  →  AgentRuntime  →  Model / Tools / Stores
```

The core (`core/`, `tools/`, `persistence/`) must **never** import FastAPI, Telegram,
Textual, or any gateway. Enforced as an invariant test in Phase 27.

### 1.3 Three kinds of things, never conflated

| Kind | Examples | Mutable? |
|---|---|---|
| **Definitions** (configuration) | `Agent`, `AgentLimits`, `SubAgentSpec`, `ToolPolicy` | frozen |
| **Runtime objects** (doing work) | `AgentRuntime`, `RunManager`, `ToolExecutor`, `EventBus`, `ContextBuilder` | stateful |
| **Persistent objects** (durable data) | `Session`, `Run`, `Checkpoint`, `MemoryItem`, `ArtifactRef`, `Message` | stored |

### 1.4 State ownership is explicit

```text
Agent         owns configuration
Session       owns durable conversation identity/metadata
Run           owns execution lifecycle
AgentState    owns current mutable execution state
ToolExecutor  owns tool execution details
MemoryStore   owns cross-session memories
ArtifactStore owns large outputs/files
EventBus      owns runtime event distribution
```

No two components mutate the same conceptual state.

### 1.5 Abstract only at infrastructure boundaries, and only when forced

Good early boundaries (implementations will change): `SessionStore`, `RunStore`,
`CheckpointStore`, `MemoryStore`, `ArtifactStore`, `EventBus`, `ToolRegistry`.
Everything else is direct code until duplication proves otherwise.

See [`design/source-design-document.md`](design/source-design-document.md) for the full
rationale (§§4, 34–37, 43, 51, 153, 163, 171, 180).

---

## 2. Target architecture

### 2.1 Layers

```text
Telegram ─────┐
Web ──────────┼──> Application ──> RunManager ──> AgentRuntime
TUI ──────────┘                                     │
                                          ┌──────────┼──────────┐
                                          ▼          ▼          ▼
                                        Model      Tools      Stores
                                                    │
                                              ToolExecutor
                                                    │
                                              ToolRegistry
                                                    │
                                                BaseTool
```

### 2.2 Object graph produced by bootstrap

```text
Application
│
├── RunManager
│      │
│      ├── AgentRuntime
│      │      ├── Agent  (definition)
│      │      ├── Model  (bound with tools)
│      │      ├── ToolExecutor
│      │      │       └── ToolRegistry ── terminal / read_file / write_file / …
│      │      └── EventBus
│      │
│      ├── SessionStore
│      ├── RunStore
│      └── CheckpointStore
│
├── MemoryStore
├── ArtifactStore
└── EventBus
```

### 2.3 Construction order (always leaves → root)

```text
Tools → ToolRegistry → ToolExecutor
Model
Stores
        → AgentRuntime → RunManager → Application → Gateways
```

### 2.4 The core loop (the thing everything else wraps)

```python
while True:
    response = await model_with_tools.ainvoke(build_context(state))
    state.messages.append(response)
    if not response.tool_calls:
        return response                       # final answer
    results = await tool_executor.execute_many(response.tool_calls)
    state.messages.extend(results)            # ToolMessages, same loop
```

Persistence, events, limits, and subagents all *surround* this loop. The loop itself
does not grow branches for them.

---

## 3. Current repo audit (2026-10-11)

### 3.1 What exists

| File | State | Notes |
|---|---|---|
| `pyproject.toml`, `Makefile`, `.gitignore` | ✅ | uv + ruff + mypy configured |
| `src/agent_stack/__init__.py` | ✅ | placeholder `main()` |
| `src/agent_stack/core/agent.py` | 🟡 | `AgentConfig` exists; no limits/subagents |
| `src/agent_stack/core/factory.py` | 🟡 | `AgentFactory` wires model/registry/executor |
| `src/agent_stack/core/runtime.py` | 🟡 | loop works; see defects below |
| `src/agent_stack/core/session.py` | 🟡 | in-memory `SessionStore` |
| `src/agent_stack/core/run.py` | ⬜ | empty `RunManager` stub |
| `src/agent_stack/tools/registry.py` | ✅ | duplicate/unknown handling correct |
| `src/agent_stack/tools/executor.py` | 🟡 | concurrent, but see defects below |
| `src/agent_stack/tools/builtins/filesystem.py` | ⬜ | stubs only |
| `src/agent_stack/tools/builtins/terminal.py` | ⬜ | empty |
| `src/agent_stack/app/bootstrap.py` | 🟡 | ad-hoc demo wiring, not a composition root yet |
| `src/agent_stack/app/application.py` | ⬜ | empty `Application` |
| `tests/` | ⬜ | does not exist |

### 3.2 Known defects to fix early

These are small but they corrupt the loop, so fix them before building on top:

1. **Tool args bug** — `tools/executor.py` calls `tool.ainvoke(tool_call)`; it must call
   `tool.ainvoke(tool_call["args"])`. (Fix in **Phase 3**.)
2. **Session duplication bug** — `core/runtime.py` loads the whole message list, appends,
   then `add_messages(..., messages)` **extends with the whole list again**, duplicating
   history on every turn. The runtime should only persist *new* messages. (Fix in
   **Phase 5**.)
3. **No package `__init__.py` files** in `core/`, `tools/`, `tools/builtins/`, `app/`.
   Add them so the installed package is import-safe. (Fix in **Phase 0**.)
4. **Fragile `AgentFactory`** — passes `event_bus=None` / `checkpoint_store=None` and
   binds tools in the factory. Reshape toward the composition root in **Phase 17**.
5. **`session_store.get_messages` returns the internal list** by reference, so callers
   mutate store state in place. Return a copy. (Fix in **Phase 7**.)

### 3.3 Naming decision

The design document uses `Agent` for the *definition*; this repo currently uses
`AgentConfig`. **Recommendation: rename `AgentConfig` → `Agent`** so the codebase speaks
the domain language (`Agent` vs `AgentRuntime` vs `AgentState`). Record it as ADR-001.
If you prefer to avoid churn, keep `AgentConfig` but be consistent everywhere.

---

## 4. Build order at a glance

```text
Milestone A — The minimal loop
  0  Foundation & tooling                ✅
  1  Model layer                          ✅
  2  Tool registry                        ✅
  3  Tool executor                        ✅
  4  Agent definition (Agent/limits)      ✅
  5  AgentRuntime loop                    ✅
  6  Errors & limits                      ✅

Milestone B — Durability & state
  7  Sessions & in-memory stores          ✅
  8  Event model & event bus              ✅
 8b Audit log on the event bus            ✅
  9  SQLite index + file transcripts      ✅
 10  Checkpoints & tool executions        ✅
 11  RunManager + RunHandle               ✅
 12  Cancellation, session lock, version  ✅

Milestone C — Real capabilities
 13  Workspace & filesystem tools         ✅
 14  Terminal tool subsystem              ✅
 15  Artifact store                       ✅
 16  Context builder & compaction         ✅
16b  Skills (SKILL.md)                    ✅
16c  Agent instructions (AGENTS.md)       ✅
16d  Memory files (MEMORY.md)             ✅
16e  Context wiring (make it real)        ✅

Milestone D — Composition & application
 17  Composition root (bootstrap.py)      ✅
 18  Application service layer            ✅
 19  Gateway contract (base.py)           ✅

Milestone E — Multi-agent & memory
 20  Subagents (delegation)               ✅
 21  Persistent memory                    ✅
 22  Planning / TODOs                     ✅

Milestone F — Gateways (**order: TUI → Telegram → Web**)
 23  TUI gateway (Textual + Rich)         ✅
 25  Telegram gateway                     ✅
 24  Web gateway (Next.js, SSE)           ✅

Milestone G — Trust & operations
 26  Approval flow                        ✅
 27  Observability & invariant tests      ✅
 28  Hardening                            ✅

Milestone H — Time travel & branching (later)
 29  Durable event log & replay           ✅
 30  Conversation branching               ✅

Milestone I — Context engineering
 31  Compaction (summarise and continue)  ✅
 32  Chat search (FTS5 + BM25)             ✅
```

> **Build order one-liner** (keep visible):
> model → messages → registry → executor → loop → errors/limits → tests →
> events → **audit log** → sessions → sqlite **index** + **file transcripts** → checkpoints →
> run manager → cancellation → filesystem → terminal → artifacts → context →
> **skills + AGENTS.md + memory files (MEMORY.md)** → bootstrap → application → gateways →
> subagents → memory → planning → approval → observability → hardening →
> **durable event log → branching (Milestone H)**.

Gateway build order detail: **TUI first (P23)**, **Telegram second (P25)**, **Web last
(P24)**. The web gateway is **Next.js + SSE** (no WebSocket) and its frontend design is
driven by the design skills in `.claude/skills/` (local only); the owner shared the design
inspiration and the style was chosen in ADR 013.

---

# Milestone A — The minimal loop

**Demo at the end:** `python -m agent_stack.app.bootstrap "What files are in ./src?"`
returns a final answer after calling a real tool, with deterministic tests proving the
loop.

---

## Phase 0 — Foundation & tooling ✅

**Goal.** A clean, importable package with a fast check loop.

**Deliverables**
- `__init__.py` in `core/`, `tools/`, `tools/builtins/`, `app/`.
- `app/config.py` — typed, frozen `AppConfig` (no scattered `os.getenv()`).
- `.env.example` (never commit real keys; the current `.env` has one — add it to
  `.gitignore` if not already).
- Confirm `make check` (lint + typecheck + test) runs green/empty.

> **Status note:** done. The environment is read in `app/config.py`, the composition root and
> entry point, the terminal tool's subprocess allow-list, and secret detection; invariant 9
> (`tests/invariants`) keeps it to exactly those files.

**Key interfaces**
```python
@dataclass(frozen=True)
class AppConfig:
    default_model: str
    workspace_root: Path
    database_path: Path
    max_concurrent_tools: int = 8
    telegram_token: str | None = None

    @classmethod
    def from_env(cls) -> "AppConfig": ...
```

**Acceptance**
- `uv run python -c "import agent_stack.core.runtime"` works with no side effects.
- `make check` succeeds.
- No module reads `os.environ` except `app/config.py`.

**Defer:** pyright (mypy is configured); multi-environment config; secrets manager.

---

## Phase 1 — Model layer ✅

**Goal.** One place that turns a model string into a `BaseChatModel`, with capability
validation.

**Why.** Provider-specific construction must not leak into the runtime. §8–9.

**Deliverables**
- `models/factory.py` — `build_model(model: str) -> BaseChatModel`.
- Capability validation was **cut as unneeded** (2026-10-11 audit): nothing consumed
  `ModelCapabilities`; tool-calling capability is exercised by the running loop itself.
  Re-add only if a real requirement forces it.
- Move the `init_chat_model(...)` call out of `core/factory.py`.

**Key interfaces**
```python
def build_model(model: str, *, temperature: float = 0) -> BaseChatModel:
    return init_chat_model(model, temperature=temperature)

@dataclass(frozen=True)
class ModelCapabilities:
    tool_calling: bool
    streaming: bool = False
    structured_output: bool = False
```

**Acceptance**
- `await build_model(cfg.default_model).ainvoke([HumanMessage("hi")])` returns an `AIMessage`.
- With tools bound, `response.tool_calls` is populated for a tool-forcing prompt.
- No provider `if/elif` anywhere outside `models/`.

**Defer:** model routing, per-tool model selection, token accounting, retries (Phase 6/27).
*Later:* token and cost accounting were built (`model.usage` events, `/cost`, budgets); a small
model for side work came with ADR 014. Model routing and retries are still not built.

---

## Phase 2 — Tool registry ✅

**Goal.** Name → `BaseTool` lookup with collision/unknown rejection. §18.

**Status.** Already implemented in `tools/registry.py`. Add tests.

**Acceptance tests**
- `test_registry_get_returns_tool`
- `test_registry_rejects_duplicate_names`
- `test_registry_raises_on_unknown_tool`

**Defer:** wrapping tools in a custom `RegisteredTool` with policy/timeout metadata
(§84) — that arrives in Phase 6/13.

---

## Phase 3 — Tool executor ✅

**Goal.** Turn model tool calls into `ToolMessage`s, concurrently, safely. §20–24.

**Fix now (defects 3.2 #1):** invoke with `tool_call["args"]`.

**Deliverables**
- `tools/executor.py` fixed + hardened:
  - lookup via registry (unknown tool → error `ToolMessage`, not a crash),
  - `asyncio.gather` for parallel calls,
  - per-call error capture → `ToolMessage(status="error")`,
  - result ordering keyed by `tool_call_id` (never completion order).

**Key interfaces**
```python
class ToolExecutor:
    def __init__(self, registry: ToolRegistry): ...

    async def execute_one(self, tool_call: dict) -> ToolMessage: ...
    async def execute_many(self, tool_calls: list[dict]) -> list[ToolMessage]: ...
```

**Acceptance tests**
- `test_executes_single_tool_call`
- `test_executes_parallel_tool_calls`
- `test_preserves_tool_call_ids`
- `test_continues_after_tool_error`
- **Parallelism proof (§138):** two tools that each `await asyncio.sleep(0.5)`
  complete in ~0.5s, not ~1.0s.

**Defer:** timeouts, policy/concurrency groups (§22), artifacts, redaction, events.

---

## Phase 4 — Agent definition ✅

**Goal.** A frozen configuration object describing an agent. §7, §100.

**Deliverables**
- Rename/finish `core/agent.py`:
  - `AgentLimits`, `SubAgentSpec`, `Agent` (or keep `AgentConfig`, see §3.3).
- **No** `state`, `memory`, `session`, `event_bus` on the definition.

**Key interfaces**
```python
@dataclass(frozen=True)
class AgentLimits:
    max_steps: int = 50
    max_tool_calls: int = 100
    max_runtime_seconds: int = 1800
    max_concurrent_tools: int = 8
    max_subagent_depth: int = 2
    max_subagents: int = 8

@dataclass(frozen=True)
class SubAgentSpec:
    name: str
    description: str
    agent: "Agent"

@dataclass(frozen=True)
class Agent:
    name: str
    model: str
    tools: tuple[BaseTool, ...] = ()
    subagents: tuple[SubAgentSpec, ...] = ()
    system_prompt: str = ""
    limits: AgentLimits = AgentLimits()
```

**Acceptance**
- Definitions are hashable/immutable; constructing an `Agent` has no side effects.
- A test asserts `Agent` exposes exactly the config fields (no runtime state).

**Defer:** filling `subagents` (Phase 20), model routing (never, per §116).

---

## Phase 5 — AgentRuntime loop + AgentState ✅

**Goal.** The explicit, boring model/tool loop with a real state object. §13–14.

**Fix now (defect 3.2 #2):** persist only newly produced messages; don't re-append the
loaded history.

**Deliverables**
- `core/state.py` — `AgentState` (v1 fields only: `messages`, `step_count`,
  `tool_call_count`, `metadata`).
- `core/runtime.py` — `AgentRuntime.execute(...)` driving the loop; `build_context(state)`.
- Runtime receives `model`, `tool_executor`, `session_store` — not a bound model, not a
  factory.

**Key interfaces**
```python
class AgentRuntime:
    def __init__(self, *, agent: Agent, model: BaseChatModel,
                 tool_executor: ToolExecutor, session_store: SessionStore): ...

    async def execute(self, *, session_id: str, user_id: str, message: str) -> AgentResult: ...
```

**Acceptance tests (read as a spec — §137)**
- `test_returns_direct_model_response`
- `test_executes_single_tool_call`
- `test_executes_parallel_tool_calls`
- `test_persists_messages_without_duplication`  ← guards the fixed defect

**Defer:** events, streaming, subagents. **Stop here and understand every line.**

---

## Phase 6 — Errors & limits ✅

**Goal.** Classify failures; guarantee the loop terminates. §43, §93.

**Deliverables**
- `core/errors.py` — hierarchy (`AgentStackError`, `ToolError`, `LimitExceededError`,
  `StepLimitExceeded`/`ToolCallLimitExceeded`, `PersistenceError`, `RunCancelled`,
  `WorkspaceEscapeError`, `CommandTimeoutError`, `SessionVersionConflictError`).
- `core/limits.py` — enforcement of `max_steps`, `max_tool_calls`, `max_runtime_seconds`.
- Executor: unknown tool → error `ToolMessage`; timeouts → error messages.

**Acceptance tests**
- `test_stops_after_max_steps`
- `test_stops_after_max_tool_calls`
- `test_stops_after_runtime_deadline`
- `test_unknown_tool_becomes_error_message`
- `test_tool_timeout_becomes_error_message`

**Done:** `RuntimeDeadlineExceeded` and `ToolTimeoutError` join the hierarchy. The runtime
enforces `max_runtime_seconds` as a wall-clock deadline around every model call and tool batch
(a provider's own `TimeoutError` is not mistaken for it); the executor enforces
`AgentLimits.tool_timeout_seconds` per call and returns an error `ToolMessage`. `tool.completed`
now carries `ok` and `error`, so the TUI can show a failed tool (✗, card stays open). Tests:
`test_stops_after_max_tool_calls`, `test_stops_after_runtime_deadline`,
`test_tool_timeout_becomes_error_message` (+ the existing step-limit and unknown-tool tests).

**Defer:** retry policies (§44–45) until a real transient failure appears.

---

# Milestone B — Durability & state

**Demo at the end:** send messages, kill the process, restart, continue the same
session; crash mid-run and resume without duplicating a completed tool.

---

## Phase 7 — Sessions & in-memory stores ✅

**Goal.** A `Session` domain object and store protocols; in-memory implementations first. §10, §16, §95.

**Deliverables**
- `core/session.py` — `Session` dataclass + `SessionStore` protocol.
- `persistence/base.py` — store protocols (`SessionStore`, `MessageStore`).
- `persistence/memory.py` — in-memory implementations (return **copies**, fixing
  defect 3.2 #5).
- `core/store.py` or `persistence` ownership: keep LangChain messages at the model boundary.

**Key interfaces**
```python
class SessionStore(Protocol):
    async def get(self, session_id: str) -> Session | None: ...
    async def save(self, session: Session) -> None: ...
    async def get_messages(self, session_id: str) -> list[BaseMessage]: ...
    async def append_messages(self, session_id: str, messages: list[BaseMessage]) -> None: ...
```

**Acceptance**
- Request 1 creates a session; request 2 with the same `session_id` continues it.

**Defer:** SQLite (next phase), session locking/versioning (Phase 12), titles heuristic
(§147).

---

## Phase 8 — Event model & event bus ✅

**Goal.** Gateways consume a stable event protocol, never raw model payloads. §30–34.

**Deliverables**
- `core/events.py` — `EventType` (StrEnum), `AgentEvent` (with monotonic `sequence`).
- `core/event_bus.py` — in-process pub/sub with `publish()` and
  `subscribe(run_id) -> AsyncIterator`.
- Runtime emits: `run.started`, `assistant.message`, `tool.started`, `tool.completed`,
  `run.completed`, `run.failed`.

**Key interfaces**
```python
@dataclass(frozen=True)
class AgentEvent:
    id: str
    type: EventType
    run_id: str
    session_id: str
    sequence: int
    timestamp: datetime
    data: dict[str, Any]
```

**Acceptance**
- `async for event in bus.subscribe(run_id): ...` yields events in sequence order.
- No raw `response.model_dump()` ever reaches a gateway (§30).

**Defer:** durable event log/replay (Phase 29 — Milestone H), branching (Phase 30),
Redis/NATS (§33). The `EventType` enum grows over time: `subagent.*` (P20) and streaming
`tool.output` (P14) are added to the same enum — one source of truth for gateways.
Every published event is also appended to the audit log (P8b): the bus is the one choke
point, so nothing can emit without leaving a trace.

---

## Phase 8b — Audit log on the event bus ✅

**Goal.** Every published event leaves a durable, greppable trace so the system is
debuggable on the fly. The bus is the single choke point — audit it there, not in each
gateway.

**Deliverables**
- `observability/audit.py` — `AuditLogger`: one append-only JSONL line per event
  (`ts`, `run_id`, `session_id`, `sequence`, `type`, capped `data`), written by the bus
  in the same `publish()` call that dispatches to subscribers — one code path, so no
  event can be emitted unlogged.
- Cap/redact `data` (never raw secrets; truncate big payloads such as tool output).
- Live debugging: `agent-stack audit tail [--run-id …] [--from-sequence …]` (follow the
  log, filter by run) plus a TUI audit pane (P23). Survives restart — it is the on-disk
  trace that the durable event log (P29) later builds on.
- Store at `<workspace>/.agent/logs/audit.jsonl`.

**Acceptance**
- After a run, every event the bus published has exactly one audit line, in sequence order.
- The audit file survives a restart and replays that run's events in order.
- `data` payloads are capped and redacted (no key material).

**Defer:** metrics/tracing/invariants (Phase 27) and full replay by
`last_event_sequence` (Phase 29).

---

## Phase 9 — SQLite index + file-based conversation store ✅

**Goal.** Durable sessions/runs **and** human-reviewable conversation transcripts, without
parking whole chats in SQLite. §35–37, §128–130, plus
[`design/conversation-storage.md`](design/conversation-storage.md).

**Decision.** The full conversation (every message, plus artifacts generated/shared in the
chat) is written to **per-session files**. SQLite holds only the **queryable index**:
session metadata, run lifecycle, and message *pointers* (offsets) — never the message
payloads or artifacts.

**Deliverables**
- `persistence/sqlite.py` — connection handling + `SqliteSessionStore`, `SqliteRunStore`,
  `SqliteMessageIndex` (index rows only).
- `persistence/conversation.py` — `ConversationStore` protocol + `FileConversationStore`:
  `transcript.md` (human-readable), `transcript.jsonl` (machine-readable, append-only),
  `session.json` (metadata mirror), `artifacts/` (files created/shared in the session).
- `persistence/serializers.py` — LangChain message ⇄ normalized JSON (human/ai/tool with
  `tool_calls` + `tool_call_id`).
- `persistence/migrations.py` — schema bootstrap (`sessions`, `runs`, `message_index`,
  `artifacts_index`).
- Swap bootstrap to the SQLite index + file store (runtime unchanged).

**Accepted layout**
```text
<workspace>/.agent/sessions/<session_id>/
├── session.json         # metadata (mirrors the SQLite row)
├── transcript.md        # human-readable, append-only
├── transcript.jsonl     # machine-readable, append-only (one message/event per line)
└── artifacts/           # generated or shared files
```

**Acceptance**
- Send messages → kill process → restart → load session → history intact → continue.
- `transcript.md` is readable and contains the whole chat + artifact references.
- Deleting a session deletes its directory (no orphaned artifacts).
- SQLite contains **no** message content (assert the column set).

**Defer:** `checkpoints`, `tool_executions`, `events` tables (Phase 10); S3; retention (§148).

---

## Phase 10 — Checkpoints & tool executions ✅

**Goal.** Semantic snapshots + idempotent tool records for recovery. §38–39, §85–86.

**Deliverables**
- `CheckpointStore` (SQLite) + `RunStore` tool-execution records.
- Checkpoint at boundaries: user msg accepted, AI msg received, tool batch completed,
  subagent completed, approval resolved, run completed.
- `ToolExecutionResult` (runtime-level) distinct from the model-facing `ToolMessage`.

**Acceptance tests**
- `test_resumes_from_checkpoint`
- `test_does_not_duplicate_completed_tool` (idempotency key = `run_id + tool_call_id`)
- Tool batch is persisted in one transaction (§130).

**Defer:** every-token checkpoints (never); distributed checkpoints.

---

## Phase 11 — RunManager + RunHandle ✅

**Goal.** Make run lifecycle first-class, separate from execution. §19, §43, §74–75.

**Deliverables**
- `core/run.py` — `Run`, `RunStatus`, `RunManager`, `RunHandle`.
- `RunManager.start/resume/cancel/recover`; `runtime.execute(run)` does the loop.
- `RunHandle.events()` and `.result()`.

**Key interfaces**
```python
class RunManager:
    async def start(self, request: RunRequest) -> RunHandle: ...
    async def resume(self, run_id: str) -> RunHandle: ...
    async def cancel(self, run_id: str) -> None: ...
    async def recover(self) -> None: ...
```

**Acceptance**
- `test_run_lifecycle_transitions` (QUEUED → RUNNING → COMPLETED/FAILED/CANCELLED)
- `test_recover_finds_incomplete_runs_on_startup` (§76)

**Defer:** background worker queue (§74) — `asyncio.create_task()` is enough for now.

---

## Phase 12 — Cancellation, session lock, versioning ✅

**Goal.** Safe interrupts and safe concurrency. §40–42.

**Deliverables**
- `core/cancellation.py` — `CancellationToken`; checked before model calls, around tool
  execution, and between loop iterations.
- `SessionLockManager` — one `asyncio.Lock` per `session_id`.
- Optimistic session version bump (compare-and-swap on save).

**Acceptance tests**
- `test_stops_when_cancelled`
- `test_concurrent_same_session_requests_serialize`
- `test_lost_update_detected_by_version_mismatch`

**Defer:** distributed locks / DB leases (only when multi-process).

---

# Milestone C — Real capabilities

**Demo at the end:** a coding agent that inspects a repo, reads/writes files, runs
tests, and keeps large output out of context.

---

## Phase 13 — Workspace & filesystem tools ✅

**Goal.** Safe, testable file operations inside a defined workspace. §28–29.

**Deliverables**
- `artifacts/workspace.py` or `core/workspace.py` — `WorkspacePolicy` (root, allow_external).
- `tools/builtins/filesystem.py` — `list_directory`, `read_file`, `write_file`,
  `edit_file`, `search_files` (each a `BaseTool`).
- All tools resolve paths against the workspace root; escape attempts rejected.

**Acceptance**
- Integration tests run against a `tmp_path` workspace, **never** the real home dir.
- `test_read_file_outside_workspace_rejected`

**Defer:** apply_patch, binary handling.

---

## Phase 14 — Terminal tool subsystem ✅

**Goal.** Async subprocess execution as a subsystem, not a one-liner. §27, §80–81.

**Done:** `run_command` (`create_subprocess_exec`, process-group + cancel kill, timeout,
stdout/stderr caps, env allowlist `PATH`/`HOME`/`LANG`, `CommandResult`), `run_command_tool`,
and the **streaming half** — live `tool.output` events via a per-call contextvar sink
(`core/tool_stream.py`) installed by the executor and published by the runtime, with
`run_command` reading stdout/stderr incrementally (incremental UTF-8 decode, bounded
emission). The TUI/Web shells panes (P23/P24) render this.

**Deliverables**
- `tools/builtins/terminal.py` — `run_command`:
  - `asyncio.create_subprocess_exec` (no implicit `shell=True`),
  - process-group creation + termination on cancel,
  - timeout enforcement, stdout/stderr caps (e.g. 32 KB),
  - environment allowlist (`PATH`, `HOME`, `LANG`),
  - `CommandResult` with `exit_code`, `stdout`, `stderr`, `duration_ms`, `timed_out`.
  - **Streaming:** stdout/stderr chunks emitted as `tool.output` events while the command
    runs (not only the final `CommandResult`), so the TUI/Web render live output —
    opencode-style running-shell panes (P23/P24). ✅

**Acceptance**
- `test_command_timeout`, `test_command_env_is_filtered`, `test_command_cwd_preserved`.
- `test_command_output_streams_as_events` — output chunks arrive as events *before*
  the command completes.
- Cancellation kills child processes.

**Defer:** sandboxing/containers, approval (Phase 26).

---

## Phase 15 — Artifact store ✅

**Goal.** Large outputs live outside model context; the model sees a reference. §25–26, §112.

**Deliverables**
- `artifacts/base.py` — `ArtifactStore` protocol + `ArtifactRef`.
- `artifacts/local.py` — `LocalArtifactStore` under `workspace/.agent/artifacts/<uuid>`.
- Executor: exceeding an output cap stores the full output as an artifact and returns a
  truncated/summarized `ToolMessage` with `artifact://<id>`.

**Acceptance test**
- `test_large_output_becomes_artifact_and_short_message`

**Done:** `LocalArtifactStore` is wired in `bootstrap` (`<workspace>/.agent/artifacts/`). A tool
result over 20 KB is stored whole, as the already-redacted text, and the model gets a 4 KB
preview plus `artifact://<id>`; the new `read_artifact` tool pages through it (ids are
validated, and a single read is clamped). Without a store, or if storing fails, the result is
truncated at 100 KB as before. Tests: `tests/unit/test_large_output_artifacts.py`.

**Known limit:** artifacts are workspace-scoped, not per-session (the local store has no
session layout), so deleting a conversation leaves its large-output artifacts behind until the
garbage collection this phase defers.

**Defer:** S3 backend, artifact garbage collection (§148–149).

---

## Phase 16 — Context builder & compaction ✅

**Goal.** Bound the model's context; never send unbounded history. §16, §52–54.

**Deliverables**
- `core/context.py` — `ContextBuilder.build(state, session)` and `.compact(...)`.
- v1 strategy: `recent window`; then `summary + recent messages`.
- *Superseded:* the recent window became a token budget (P16e and later), and `.compact()` plus
  the in-memory summary of dropped messages were replaced by saved compaction in P31.
- Compaction preserves goals, decisions, files changed, failures, open questions.

**Acceptance tests**
- `test_context_builds_system_plus_recent_messages`
- `test_large_conversation_is_compacted` (fake summarizer model)

**Defer:** vector/semantic retrieval (§57), automatic memory extraction.

---

## Phase 16b — Skills (`SKILL.md`) ✅

**Goal.** Discover and progressively load file-based skills. Full design:
[`design/skills-and-instructions.md`](design/skills-and-instructions.md).

**Deliverables**
- `skills/models.py` (`SkillMetadata`, `Skill`, `SkillResource`), `skills/loader.py`
  (discover + parse YAML frontmatter + name validation), `skills/registry.py`.
- Discovery roots: `<workspace>/.agent/skills/`, repo `skills/`, repo `.claude/skills/`
  (the design skills live here — local only, gitignored), `~/.claude/skills/`
  (opt-in).
- Level-1 metadata appended to the system prompt (name + description only).
- Tools: `load_skill(name)` (level 2 body), `read_skill_resource(name, path)` (level 3).
- `scripts/` execute only via the terminal tool (P14) — never a bespoke executor.

**Acceptance**
- `test_discovers_skill_metadata`, `test_rejects_invalid_skill_name`,
  `test_rejects_missing_description`, `test_loader_returns_body_not_metadata`,
  `test_read_resource_reads_only_within_skill_dir`,
  `test_system_prompt_lists_skill_metadata_only`.

**Smoke (real model):** place a `SKILL.md` whose answer is only in its body, ask a question,
confirm the model calls `load_skill` and answers. Full spec: agentskills.io/specification.

**Defer:** `allowed-tools` enforcement (metadata only first).

---

## Phase 16c — Agent instructions (`AGENTS.md`/`CLAUDE.md`) ✅

**Goal.** Load project instructions with nearest-file-wins precedence.

**Deliverables**
- `context/instructions.py` — `InstructionsLoader.load(target_dir, stop)` resolving every
  `AGENTS.md` (fallback `CLAUDE.md`, plus the personal variants `CLAUDE.local.md` and
  `.claude/CLAUDE.md`) from the working dir up to the root, nearest first.
- `ContextBuilder` composes: base prompt → instructions (delimited) → skills metadata →
  memory → recent messages.
- Size cap (8 KB/file, 32 KB total) + `(path, mtime)` cache.

**Acceptance**
- `test_loads_root_agents_md`, `test_nested_agents_md_nearest_wins`,
  `test_supports_claude_md_fallback`, `test_instruction_size_cap_enforced`,
  `test_instructions_injected_into_system_prompt`.

**Smoke (real model):** an `AGENTS.md` states a project convention; ask a question whose
correct answer follows the convention and confirm the model obeys it.

**Defer:** `@imports` directives and monorepo per-package permissions.

---

## Phase 16d — Memory files (`MEMORY.md`) ✅

**Goal.** Support the file-based memory convention: a `MEMORY.md` index + topic files,
with the same three-level progressive disclosure as skills (P16b). Full survey + mapping:
[`design/file-capabilities.md`](design/file-capabilities.md).

**Deliverables**
- `memory/files.py` — `MemoryIndex` loader over `<workspace>/.agent/memory/` (a bare
  `MEMORY.md` at the workspace root is the fallback); index = one short line per topic,
  capped like the skills metadata.
- Level 1: the index appended to the system prompt (delimited), same slot as instructions
  and skills metadata in the `ContextBuilder` composition.
- Tools: `read_memory_topic(name)` (level 2) and `write_memory_topic(name, content)`
  (level 3) — path-scoped to the memory root, traversal rejected; writes keep the index
  in sync and stamp an `updated` timestamp (convention-compatible).

**Acceptance**
- `test_memory_index_loaded_capped` — index lines, not topic bodies, reach the prompt.
- `test_read_topic_on_demand_scoped_to_memory_dir` — traversal rejected.
- `test_write_topic_updates_index_entry`.

**Defer:** automatic memory extraction and the managed store (Phase 21); topic GC/merge.

---

## Phase 16e — Context wiring (make it real) ✅

**Goal.** The file-capability pieces (P16 builder, P16b skills, P16c instructions, P16d
memory) are built and unit-tested, but **nothing wires them into a run**: the runtime
sends only `SystemMessage(agent.system_prompt)` over the *entire* history — no
instructions, no skills metadata, no memory index, and no recent-window/compaction. This
phase makes the composition real, so what the model actually sees is
`base prompt → instructions → skills metadata → memory → recent window`, bounded.

**Deliverables**
- `context/composer.py` — a `SystemPartsProvider` that produces the ordered resident parts
  (instructions from `InstructionsLoader`, skills metadata from `SkillRegistry`, memory
  index from P16d) for a workspace.
- `AgentRuntime` uses `ContextBuilder` for **both** the system parts and the history:
  `compact()` the loaded history, then `build(system_parts=parts, history=history+new)` each
  iteration. With nothing injected it stays behaviour-preserving for short conversations.
  The provider seam lives in `core` (a `Protocol`) so `core` never imports `context`.
- `bootstrap.py` wires the loaders/registries + the provider into `AgentFactory` →
  `AgentRuntime`, and attaches the `load_skill`/`read_skill_resource` (and P16d memory)
  tools to the default agent. Discovery roots per the P16b/P16c/P16d specs.

**Acceptance**
- `test_agents_md_reaches_model_prompt` — an `AGENTS.md` convention appears in the model input.
- `test_skill_metadata_reaches_model_prompt` — skill names/descriptions are resident; bodies are not.
- `test_history_window_caps_model_input` — a long history is windowed (or compacted), not sent whole.
- `test_memory_index_reaches_model_prompt` — memory index lines reach the prompt (with P16d).

**Defer:** semantic/vector retrieval; per-session instruction overrides.

**Discovered — SKILL.md parser is a strict subset.** Real `SKILL.md` files use fuller YAML than P16b accepts, so at the composition boundary (`strict=False`) several vendored skills are silently skipped: `ponytail` uses a `>` block-scalar description, `web-design-guidelines` a nested `metadata:` map, and `image-to-code-skill`/`taste-skill` have a frontmatter name that differs from the directory name. Follow-up: accept `|`/`>` block scalars for `description`, tolerate (parse-and-ignore) a nested `metadata:` map, and decide whether a dir/name mismatch is a hard error or a warning. Until then only skills that match the subset reach the model's resident metadata.
*Partly resolved later:* the frontmatter is parsed as real YAML (block scalars, nested
`metadata`, unknown keys), so `ponytail` and `web-design-guidelines` load. A frontmatter name
that differs from its folder is still an error, so `image-to-code-skill` (name `image-to-code`)
and `taste-skill` (name `design-taste-frontend`) are still skipped (open; rename the folders or
relax the rule).

---

# Milestone D — Composition & application

**Demo at the end:** one `create_application()` call wires everything and can be driven
by a trivial CLI/in-process client.

---

## Phase 17 — Composition root ✅

**Goal.** `app/bootstrap.py` is the single wiring diagram in code. §24–26, §98.

**Deliverables**
- Rewrite `app/bootstrap.py` into `create_application(config) -> Application`:
  infrastructure → stores → event bus → agent factory → runtime → run manager →
  application.
- `AgentFactory` constructs a runtime (model, registry, executor) from an `Agent`
  definition and shared stores; supports child runtimes for subagents later. §7, §32.

**Key interfaces**
```python
def create_application(config: AppConfig) -> Application:
    session_store = SqliteSessionStore(config.database_path)
    run_store = SqliteRunStore(config.database_path)
    checkpoint_store = SqliteCheckpointStore(config.database_path)
    memory_store = SqliteMemoryStore(config.database_path)
    artifact_store = LocalArtifactStore(config.workspace_root / ".agent" / "artifacts")
    event_bus = InMemoryEventBus()

    factory = AgentFactory(
        session_store=session_store, run_store=run_store,
        checkpoint_store=checkpoint_store, memory_store=memory_store,
        artifact_store=artifact_store, event_bus=event_bus,
    )
    runtime = factory.create(PERSONAL_AGENT)
    run_manager = RunManager(runtime=runtime, run_store=run_store,
                             checkpoint_store=checkpoint_store, event_bus=event_bus)
    return Application(run_manager=run_manager, session_store=session_store,
                       memory_store=memory_store, event_bus=event_bus)
```

**Acceptance**
- Swapping SQLite → in-memory requires changing **only** bootstrap.
- `test_bootstrap_creates_full_object_graph`.

**Defer:** any DI framework (§99) — plain constructors only.

---

## Phase 18 — Application service layer ✅

**Goal.** The stable boundary every gateway talks to. §21–23, §68.

**Deliverables**
- `app/application.py` — `Application` with:
  - `send_message(envelope, *, parent_run_id=None) -> RunHandle`
  - `cancel_run(run_id)`, `get_session(session_id)`, `subscribe(run_id)`.
  - `send_input(run_id, message)` is **deferred to P20** (needs the per-run input channel).
- `IdentityResolver` (`gateway:id`) + `SessionResolver` (one default session per principal;
  deterministic and restart-safe). §11, §103.

**Acceptance**
- A fake gateway can drive a full run using only `Application`; it never imports `core`.

**Defer:** authorization policy depth (start with an owner check).

---

## Phase 19 — Gateway contract ✅

**Goal.** One inbound shape and one outbound protocol. §60–61.

**Deliverables**
- `gateways/base.py` — `MessageEnvelope` (principal, session, text, metadata), `Gateway`
  protocol (`handle_message(application, envelope) -> AsyncIterator[AgentEvent]`).
  Rendering hints deferred until an event needs one.
- Gateways convert native protocol → `MessageEnvelope` and consume `AgentEvent`.

**Acceptance**
- A test constructs an `Application`, feeds a `MessageEnvelope`, and asserts events.

**Defer:** gateway-specific rate limits (§126) until a gateway exists.

---

# Milestone E — Multi-agent & memory

---

## Phase 20 — Subagents ✅

**Goal.** Delegation reuses the normal tool path — and a subagent is as visible and
steerable as the main agent. §46–51.

**Deliverables**
- `subagents/delegation.py` — a `delegate(agent_name, task)` tool that resolves a child
  `Agent` via `AgentFactory`, runs a child run under `parent_run_id` (own `run_id`, own
  `RunHandle`), and returns a compact `AgentResult`/summary (not the full transcript).
- **Full transparency:** the child run publishes every event to the same bus tagged with
  `parent_run_id` — the TUI/Web subscribe to the child's `RunHandle` and render its
  messages, tool calls, and progress exactly like the main agent (Claude Code / opencode
  subtask views), not just a terminal summary.
- `RunStore.list_runs(parent_run_id)` lists children; `RunStore.get(run_id)` exposes a
  child's status + step/tool counts for the parent's tools.
- **Interception — the parent agent can steer a running child via tools:**
  - `subagent_status(run_id)` → status, step/tool counts, recent events;
  - `subagent_interrupt(run_id)` → cancels the child run (P12); the parent receives the
    matching `subagent.completed` event with the outcome;
  - `subagent_feed(run_id, message)` → injects a message into the running child.
- **Injection mechanism:** every run has an input channel (asyncio queue); the runtime
  loop checks it at the same boundaries as cancellation (P12) — before a model call and
  between tool batches — and inserts the injected `HumanMessage` into the child's
  context. `Application.send_input(run_id, message)` is the service-level entry point
  (P18); the UI cancel/input controls use the same call surface.
- Enforce `max_subagent_depth`, `max_subagents`.

**Acceptance tests**
- `test_runs_subagent`
- `test_subagent_events_stream_to_parent_scope` — every child event carries
  `parent_run_id`
- `test_subagent_status_reports_progress`
- `test_subagent_interrupt_cancels_child`
- `test_subagent_feed_injects_message_mid_run`
- `test_enforces_subagent_depth`
- `test_parallel_subagents_run_concurrently`

**Defer:** specialized researcher/coder definitions until the mechanism works (§115);
mid-run *pause-and-resume* beyond cooperative input injection.

---

## Phase 21 — Persistent memory ✅

**Goal.** Cross-session facts, explicitly managed. §56–59.

**Deliverables**
- `memory/models.py` — `MemoryItem`; `memory/store.py` — `MemoryStore` protocol + SQLite
  implementation (key/value + keyword search, namespaces).
- `tools/builtins/memory.py` — `remember`, `recall`, `forget`.

**Acceptance**
- Facts persist across sessions; `recall` finds by keyword; namespaces isolate data.

**Done:** `memory/models.py` (`MemoryItem`), `memory/store.py` (`MemoryStore` protocol +
`SqliteMemoryStore` over a new `memory_items` table, schema v3), and the `remember` / `recall` /
`forget` tools (`tools/builtins/memory.py`) wired in `bootstrap`. Facts live in the run user's
namespace (`user:<id>`), so they carry across sessions and are invisible to other users. Recall
is a case-insensitive match of *every* query word against key and value (SQL wildcards are
literal); an empty query lists everything. These are short keyed facts, distinct from the
file-based memory topics (P16d), which hold longer project notes. Tests:
`tests/unit/test_persistent_memory.py`.

**Defer:** vector embeddings, automatic extraction (§56, §171).

---

## Phase 22 — Planning / TODOs ✅

**Goal.** Small explicit task list the model can manage. §55.

**Deliverables**
- `core/state.py` — add `TodoItem`/`TodoStatus`.
- `tools/builtins/todos.py` — `todo_list`, `todo_add`, `todo_complete`.

**Acceptance**
- Todos survive a checkpoint and resume.

**Done:** `TodoItem` / `TodoStatus` in `core/state.py`; `persistence/todos.py` (`SqliteTodoStore`,
new `todos` table, schema v4, removed when its session is deleted); `todo_list` / `todo_add` /
`todo_complete` tools (`tools/builtins/todos.py`) wired in `bootstrap`. Todos belong to the
*session*, not to a run, so they survive a checkpoint resume, a later message and a restart; ids
are per session and never shared by concurrent adds. Tests: `tests/unit/test_todos.py`.

**Defer:** sophisticated planners, DAG workflows.

---

# Milestone F — Gateways

---

## Phase 23 — TUI gateway (Textual + Rich) ✅  ← FIRST gateway to build

**Done (current state):** `gateways/tui/` is `app.py` (the Textual app and event routing),
`widgets.py`, `theme.py`, `styles.tcss` and `render.py` (small text helpers shared with the REPL);
`gateways/cli.py` is the line REPL; bare `agent-stack` launches the TUI. The chat streams user and
assistant messages, collapsible thinking and tool cards (live spinner, duration, capped and
diff-coloured shell output, ✓/✗), and subagent rows updated in place; a sidebar shows session,
context usage, activity, subagents and workspace. The look is two named true-colour themes
(ADR 012, which partly supersedes ADR 011's terminal-native look), the layout is responsive
through Textual breakpoints, and the composer is multi-line with history. `ctrl+n` starts a fresh
draft (stored on the first message), `ctrl+o` opens a picker that replays and deletes stored
conversations (`SessionStore.list_for_owner`, `Application.new_session` / `list_sessions` /
`set_session_title` / `session_history` / `delete_session`), and gated tool calls open an approval
dialog (P26). The boundary holds (gateways import only `Application`, `gateways.base` and
`core.events`, enforced by `tests/unit/test_gateway_contract.py` and the invariants suite).
Conventions: `docs/design/tui.md`.

**Also:** `model.reasoning` events (the runtime lifts the model's thinking out of
`content_blocks`/`additional_kwargs` and emits it before the answer) render as collapsed
"thinking" cards. The workspace defaults to the current directory (ADR 010), so the agent and
its subagents read the project you launch it in.

> The deliverables below are the **original plan**. The separate Subagents / Shells / Audit panes
> were replaced by inline cards and rows in the conversation (the audit log is a file:
> `.agent/logs/audit.jsonl`); per-child cancel is the palette's *Interrupt last subagent*.

**Goal.** Prove the whole stack locally with a real terminal UI. §66, §143.
**Stack decision:** [`Textual`](https://textual.textualize.io) app with
[`Rich`](https://rich.readthedocs.io) rendering — no other gateway depends on this choice.
**Design inspiration (user-specified):** **opencode's TUI** — session/run list, live
streaming output, command palette, running-shell status, agent/task list — and
**langchain's coding agent** — chat-shaped message flow with tool-in-progress states.

**Deliverables**
- `gateways/tui/` — a Textual app:
  - chat pane (Rich-rendered message blocks: human/assistant/tool), input box,
    run/tool status side panel, command palette.
  - consumes `AgentEvent`s from `RunHandle.events()` (P11) and renders them live;
    **never touches tools/models/stores**.
  - **subagent pane (full transparency + interception):** active subagents stream their
    own message/tool activity live — rendered exactly like the main chat — with per-child
    **cancel** and **send-input** controls in that pane (P20); a subagent picker
    (`/agent <name>` or @-mention in the input) delegates a task, Claude Code/opencode
    style.
  - **running shells pane:** live stdout/stderr from `tool.output` events (P14),
    per-command exit status, cancel control — opencode-style spawned-task list.
  - **audit/debug pane:** live event stream + tail of the audit log (P8b) for
    on-the-fly debugging.
  - `Ctrl+C` → cancel current run (`Application.cancel_run`); another `Ctrl+C` quits.
- Keep a thin `gateways/cli.py` REPL for scripting/debug (same event consumption).

**Acceptance**
- Interactive TUI session: messages stream in, tool calls show as status lines,
  subagent runs nest under their parent, shell commands stream live output and exit
  status, and the audit pane tails a run's events without blocking chat.
- Cancelling a run stops tool/kill chains (P12 cancellation) and returns to the input.
- Same `session_id` continues a conversation started earlier.

**Defer:** execution trees beyond subagents + shells until the events prove out.

---

## Phase 25 — Telegram gateway ✅  ← SECOND gateway to build

**Goal.** Adapter only; quirks stay at the edge. §64–65.

**Deliverables**
- `gateways/telegram/` — update → `MessageEnvelope` → `Application`; debounced status
  rendering; `/new`, `/sessions`, `/cancel` commands (§146).
- External-message idempotency key mapping (§105).

**Acceptance**
- Same `session_id` continues a conversation started in the TUI gateway.

**Done:** `gateways/telegram/` (`api.py` a minimal Bot API client over `httpx`, no new dependency;
`gateway.py` the adapter; `agent-stack telegram` to run it). Long-polling; an **allow-list of
Telegram user ids is mandatory** (`AGENT_STACK_TELEGRAM_USERS`; the bot runs tools on this
machine, so strangers get no reply and the process refuses to start without one). Commands:
`/new`, `/sessions` (inline buttons to switch, with a two-message recap), `/cancel`, `/help`.
A run shows one status message edited at most every 1.5 s (debounced for Telegram's rate
limits), replies are split under the 4096-character limit, and gated tool calls ask with
inline **Approve / Reject** buttons (answered through `Application.decide_approval`, so the
run/session/principal binding still applies). Idempotency: each update id and message id is
acted on once, and the message key rides along as `MessageEnvelope.metadata["idempotency_key"]`.
`AGENT_STACK_TELEGRAM_PRINCIPAL=tui:local-user` makes the bot share the TUI's identity, so a
conversation started in the TUI is continued from Telegram (the acceptance criterion).
`TELEGRAM_BOT_TOKEN` is never logged: errors carry no URL and `redact` knows the token shape.
Tests: `tests/unit/test_telegram_gateway.py`.

**Defer:** webhooks (long-polling is enough for one owner), media, group-chat behaviour.

---

## Phase 24 — Web gateway (Next.js + SSE) ✅  ← LAST gateway to build

**Goal.** HTTP commands + **SSE** event stream. §62–63.
**Stack decisions** (user-specified):
- **Next.js** frontend (`web/` app) — design driven by the **vendored design skills in
  `.claude/skills/`** (awesome-design-md, image-to-code, taste-skill,
  web-design-guidelines, playwright-cli; local only, gitignored). Style chosen in ADR 013.
- **SSE, not WebSocket**, for the event stream.

**Deliverables**
- Backend `gateways/web/app.py` — FastAPI routes:
  `POST /sessions`, `GET /sessions`, `POST /sessions/{id}/messages`,
  `GET /sessions/{id}`, `GET /runs/{id}/events` (**SSE**, resume from
  `Last-Event-ID`/`last_event_sequence`), `POST /runs/{id}/cancel`.
- `web/` — Next.js app consuming `POST /sessions/{id}/messages` + the SSE stream;
  design system anchored on the vendored skills; visual regression via
  playwright-cli skill. Same live affordances as the TUI: a subagent tree (from
  `subagent.*` events, with delegate-from-the-UI) and streaming shell output (from
  `tool.output`), all fed by the same SSE stream.
  *As built:* the app, subagent cards, live tool output, approvals, history and files panels
  are done and were checked in a real browser. **Not built:** the visual regression suite and
  delegate-from-the-UI (the agent delegates; the UI only shows it).
- Add `fastapi`/`uvicorn` to dependencies only now; Node toolchain is managed via the
  Next.js app's own `package.json`.

**Acceptance**
- `send_message` returns `run_id`; SSE replays from `last_event_sequence` (§104).
- UI survives reconnect: after a dropped SSE connection it resumes from the last
  received event id.

**Defer:** SSE-based approvals (Phase 26). *Later: done; approval cards arrive on the SSE stream
and are answered with `POST /runs/{run_id}/approvals/{approval_id}`.*

---

# Milestone G — Trust & operations

---

## Phase 26 — Approval flow ✅

**Goal.** Human-in-the-loop for dangerous tools, gateway-agnostic. §78–79, §124–125.

**Deliverables**
- `core/approval.py` — `ApprovalRequest`, `ApprovalStatus`.
- `ToolPolicy.requires_approval`; run enters `WAITING_APPROVAL`; emits
  `approval.required`; the run waits in-process and `Application.decide_approval` answers it.
- Validate `approval_id + run_id + session_id + principal_id` binding (§125).

**Acceptance tests**
- `test_requires_approval_pauses_run`
- `test_approval_resumes_and_executes`
- `test_rejected_approval_returns_error_tool_message`
- `test_approval_cannot_be_replayed_against_another_run`

**Done:** `core/approval.py` (`ApprovalBroker`, `ApprovalRequest`, `ApprovalStatus`, `ToolPolicy`);
`Agent.tool_policy`; the runtime asks before each gated call (one at a time), emits
`approval.required` / `approval.resolved`, and turns a rejection into an error `ToolMessage`
without running the tool; the run manager mirrors the pause as `RunStatus.WAITING_APPROVAL`.
`Application.decide_approval` checks that the decision's run, session and principal match the
request and that it is still pending, so it cannot be replayed. Time spent waiting is added back
to the run deadline. A cancelled run marks its request `CANCELLED`. Config: `approval_tools`
(`AGENT_STACK_APPROVAL_TOOLS`, default `run_command, write_file, edit_file`; empty runs
unattended). Gateways: the TUI shows an approval dialog (`y` approves; `n`/`esc` reject; no Enter
shortcut) and the line REPL asks on the console and rejects by default when it has no approver.
Also fixed: a hard cancel emits no terminal event, which left the TUI stuck on "cancelling…".
Tests: `tests/unit/test_approval.py` (+ TUI and CLI tests).

**Defer:** command classification beyond a simple flag. *Later: superseded by auto mode (rules,
then the Jev classifier, then the human; now the default approval mode).*

---

## Phase 27 — Observability & invariant tests ✅

**Goal.** Make the system debuggable and lock in the invariants. §87–89, §92.

**Deliverables**
- `observability/audit.py` already exists from P8b — the event-derived structured trace;
  this phase adds the rest.
- `observability/logging.py` — structured JSON logs keyed by `run_id`/`session_id`.
- `observability/metrics.py` + `tracing.py` — counters/timings (§88), run trace identity.
- `tests/invariants/` — encode §92 as tests:
  1. every executed tool call has one matching `ToolMessage`,
  2. a session has at most one active run,
  3. a completed run is immutable,
  4. messages are append-only,
  5. mutating tool execs have an idempotency identity,
  6. every run terminates via a known path,
  7. **gateways never execute tools directly**,
  8. **core never imports Telegram/FastAPI/Textual** (import-guard test).

**Acceptance**
- All invariants pass; no secret appears in logs (§81, §87).

**Done:** `observability/redact.py` (one `redact()` for provider keys, bearer tokens, JWTs,
`name=value` pairs, private-key blocks and known env secret values; the audit log now redacts
values, not just key names), `observability/logging.py` (`JsonLogFormatter`, `configure_logging`
-> `<workspace>/.agent/logs/agent.jsonl`, `EventLogger`), `observability/metrics.py` (counters and
timings derived from events), `observability/sinks.py` (`FanoutSink`: audit + log + metrics hang
off the one bus choke point, so the runtime carries no instrumentation). Run trace identity is
`trace_id` on every log line (the root run of a delegation chain) beside `run_id` / `parent_run_id`,
so there is no separate `tracing.py`. `tests/invariants/test_invariants.py` encodes all eight
invariants; `tests/unit/test_observability.py` includes the no-secret-in-any-log acceptance test.

**Defer:** OpenTelemetry exporters, dashboards.

---

## Phase 28 — Hardening ✅

**Goal.** Survive the ugly paths. §22, §148–149.

**Test matrix (§164 Phase 22)**
```text
process crash          database failure        duplicate gateway message
provider timeout       provider 429            tool timeout
tool exception         concurrent session access subagent failure
subagent timeout       cancellation            large output
large conversation     gateway reconnect       approval race
```

**Also**
- Secret redaction for tool output (§82).
- Data retention + deletion semantics (§148–149).

**Acceptance**
- The hardening matrix is green; a documented delete-session path leaves no orphans.

**Done (partly):** `tests/unit/test_hardening.py`.
- A failed or cancelled run now saves a **consistent prefix** of its messages (the user's message
  and every completed assistant/tool pair, never an assistant message whose tool calls got no
  result, which a provider would reject on the next turn). A storage error while saving never
  replaces the original error. A real crash (process death) still saves nothing and resumes from
  the checkpoint as before.
- What the model sees of a tool result is scrubbed of known env secrets and common secret shapes
  (`redact(..., pairs=False)`: the generic `name = value` rule stays off so source code the agent
  must read is not mangled) and capped at `MAX_OUTPUT_BYTES` (100 KB); error text gets the same.
- Session deletion removes the row, runs, message and artifact indexes, checkpoints, tool
  executions, todos and the transcript directory (tested for orphans; the large-output files
  from P15 are workspace-scoped and outlive the session, see P15); persistent memory
  facts are the *user's*, so they deliberately outlive the conversation.
- Matrix rows covered by tests: provider timeout, provider 429, tool timeout, tool exception,
  concurrent session access, cancellation, approval race (decide twice or against another
  run), large output, process crash (`recover`, now including runs paused for approval).
- Also covered: a **long conversation** (the model window never begins on a tool result whose
  call was cut off, which providers reject; this was a real bug in `ContextBuilder`), a **failing
  checkpoint database** (the run fails cleanly, the user's message is kept, the next run is
  unaffected), and a **subagent failure or timeout** (reported to the parent as a result, never
  raised). Gateway reconnect is the Telegram poll loop's backoff; a duplicate gateway message is
  the Telegram update/message-id dedupe (the TUI and REPL have no redelivery).
- **Known limit:** if recording a tool batch fails *after* the tools ran, a same-`run_id` retry
  could run a mutating tool again; the failed run saves no tool results, so the history stays valid.

---

# Milestone H — Time travel & branching (later phases)

> These are **documented now, implemented later** (after the gateways land). They turn the
> event bus + conversation store into a *timeline* you can rewind and fork.
> See [`design/event-log-and-branching.md`](design/event-log-and-branching.md).

---

## Phase 29 — Durable event log & replay ✅

**Goal.** The event bus captures **all** events durably so any past moment can be
revisited. Extends P08 (in-memory only today) — this is the "back to the past" layer.

**Deliverables**
- `persistence/event_log.py` — `events` table (append-only): `(id, run_id, session_id,
  parent_branch_id, sequence, type, timestamp, data_json)`. **Design tension:** replay
  needs payloads, but the SQLite-is-index rule (ADR-008) forbids conversation content in
  SQLite. Resolution: the durable log stores **structured event metadata + references**
  (offsets into `transcript.jsonl`); payloads rehydrate from the file store. Exact schema
  decided during the phase.
- `EventLogStore` protocol + SQLite impl; `EventStoreBus` — an `EventBus` wrapper that
  publishes in-memory **and** appends to the log.
- Replay API: `bus.replay(run_id) / replay(session_id, up_to_sequence) ->
  AsyncIterator[AgentEvent]`; `GET /runs/{id}/events` (P24 SSE) can resume from any
  past event id.

**Acceptance**
- Every event type P08 emits is found in the log after a run.
- Replay reproduces a run's events exactly, in sequence order.
- Events survive process restart (log read from SQLite + JSONL offsets).

**Defer:** event compaction, retention, event-level permissions.

---

## Phase 30 — Conversation branching ✅

**Goal.** Fork a conversation at any event and run the branch — "go back in the past and
branch" — like a versioned timeline, not a linear chat.

**Deliverables**
- Branch model: a conversation is a **tree of branches**; each branch is a linear
  sequence of events/messages rooted at a parent event.
- `SessionStore`/`ConversationStore` additions:
  - `Session.branch_of: str | None`, `Session.branch_point_event_id: str | None`;
  - `branch(session_id, branch_point) -> Session` clones messages up to the fork point
    (files: new `sessions/<id>` dir with copied prefix of `transcript.jsonl`/`.md`).
  - message `seq` stays monotonic **within a branch**; `(session_id, branch_id)` keys the
    message index.
- Runtime: starting a run on a branch continues from the branch point; a queued run that
  started earlier never writes into a forked sibling (per-session lock, P12).

**Acceptance**
- `test_branch_forks_at_event_and_preserves_prefix`
- `test_branch_and_parent_diverge_after_fork`
- `test_branch_writes_do_not_touch_parent`
- TUI/Web: an event in the past can be forked and continued (UI affordance, P23/P24+).
  *Done later* with the history work: TUI rewind picker and branches view, web History panel,
  Telegram `/rewind`.

**Defer:** branch merging/diffing, GC of dead branches.

---

# Milestone I — Context engineering

## Phase 31 — Compaction (summarise and continue) ✅

**Goal.** A long conversation keeps working: past a threshold the conversation so far is replaced
by one saved summary, the way Claude Code (auto-compact, `/compact [instructions]`) and opencode
(`session.summarize`, messages before the summary filtered out) do it.

**Where context engineering stood before this phase** (P16, P16e and later fixes): resident
system parts (base prompt, AGENTS.md/CLAUDE.md, skill metadata, MEMORY.md index) capped at 64 KB;
a token budget (`AGENT_STACK_CONTEXT_BUDGET`, 120k) with an estimate that counts tool schemas and
is calibrated against the provider's reported input tokens; over budget, old tool results are
cleared (newest 6 kept) and then the oldest messages dropped with a note, the run's request
always pinned first. The small model summarised *dropped* messages, but only in memory for one
run: the next run, or a restart, dropped and summarised again, and with no small model nothing
was summarised at all.

**Deliverables**
- `core/context.py`: `COMPACTION_HEADER`, `compaction_message`, `after_compaction` (the latest
  summary and the messages after it), `ContextBuilder.over_threshold` (`compact_at`, 0.8 of the
  budget). The old `compact()` (40-message window) and the in-memory dropped-message summary are
  gone; dropping stays as the fallback.
- `core/runtime.py`: before each model call, past the threshold, the conversation's own model
  (it follows `/model`) gets exactly the input it was about to see plus `COMPACT_PROMPT` at the
  end (request, facts, files, errors, every user message, decisions, pending, current work, next
  step); nothing is re-rendered or cut, and a prompt cache can reuse the prefix. The marker is
  appended to the run's messages, so it is saved with them; `context.compacted` and the call's
  `model.usage` are emitted. One failure turns it off for that run (dropping is the fallback).
  `AgentRuntime.compact(session, focus)` does it on demand under the session lock.
- The files the summarised messages read or edited (up to 5, 12k chars each, inside the
  workspace only) are stored with the marker as they are at that moment and shown to the model
  as the first message after it, as Claude Code re-attaches recently read files.
- `Application.compact_session`; `/compact [focus]` in the TUI, line REPL, Telegram and web
  (`POST /sessions/{id}/compact`). Auto-compaction shows live in every gateway; a saved one is a
  `compaction` history row when a chat is reopened. `AGENT_STACK_COMPACT_AT` (default 0.8,
  `off`). The small model is no longer involved (it still writes `read_chat` summaries).

**Acceptance**
- `test_past_the_threshold_the_own_model_summarises_and_the_summary_is_saved` (the real input
  plus the prompt; files re-attached; one summary across two runs; nothing deleted)
- `test_a_failed_summary_falls_back_to_dropping_and_is_not_retried`
- `test_compact_now_uses_the_focus_records_its_cost_and_runs_once`
- `test_files_outside_the_workspace_are_never_attached`
- `test_compaction_marker_hides_everything_before_it`, `test_over_threshold_…`
- gateway `/compact` tests (TUI, Telegram, web).

**Defer:** chunked summaries for a conversation whose bounded window still loses its start
(the window keeps the newest messages that fit the budget, older ones are dropped with a note).

---

## Phase 32 — Chat search (SQLite FTS5 + BM25) ✅

**Goal.** `find_chats` searches the real text of every conversation and ranks it, instead of
substring-matching the newest 200 chats and listing them newest first.

**Deliverables**
- `persistence/search.py`: `ChatSearchIndex` over an FTS5 table `chat_fts` (one row per message:
  `said` = user/agent text, compaction summaries and the title; `tool` = tool calls and output,
  cut at 4k), porter stemming, BM25 with `tool` weighted 0.3; query words and "quoted phrases"
  are quoted and OR-ed so FTS5 syntax is never an error; up to 3 snippets per chat.
- Lazy sync: before a search, each chat whose transcript size/mtime or title changed since it
  was indexed is re-read (`chat_fts_state`), so appends, rewinds, forks and renames need no
  write hooks. The transcripts stay the source of truth; the index can be deleted at will.
- `Application.chat_index` (wired in bootstrap); `ChatLibrary.find` searches all of a user's
  chats (subagent sessions excluded) through it.

**Acceptance** (`tests/integration/test_chat_search.py`)
- discussed beats scrolled-past; unrelated chats are left out; snippets bracket the match
- stems, titles, phrases and FTS5 operator words in a query
- only changed chats are re-read; another owner's chats are never returned

**Defer:** semantic (embedding) search for queries that share no words with the chat;
opening a chat at the matched message.

---

## Phase 33 — Queued messages and TUI tabs ✅

**Goal.** A message sent while the agent works is not refused: it reaches the model at its next
step. The TUI holds several conversations at once, each running on its own.

**Deliverables**
- Runtime: every run's inbox is drained before each model call and once more after the final
  answer; a message found then is answered in the same run. An empty final check closes the
  inbox, so a later `send_input` returns `False` instead of being lost. `input.received` is
  emitted for each message read. `RunManager.withdraw_input` / `Application.withdraw_input` take
  back a message the model has not read. Monitor notes are passed to the runtime directly, no
  longer through the inbox.
- Gateways: TUI, web (`POST /runs/{id}/input`, `…/input/withdraw`) and Telegram queue a message
  into the working run. TUI and web show it as "queued" until it is read, can take it back to edit
  or cancel (TUI: up arrow on an empty prompt; web: edit / cancel buttons), and hand unread ones
  back to the composer when a run is stopped.
- TUI tabs (`gateways/tui/tabs.py`): each open conversation is a `Conversation` pane holding its
  own run state; events route to the pane that started the run. The top bar is only tabs (title
  cut with `...`, spinner while running, `?` while waiting). `ctrl+n` new tab, `alt+1..9`,
  `ctrl+pageup/pagedown`, `/close`. The sidebar shows one context meter (the size the model last
  reported for the main run, `~` estimate before that), status, spend, then model, approval mode
  and workspace. The small model's title replaces the first message once it lands.

**Acceptance**
- `test_a_message_that_arrives_during_the_final_step_is_answered_in_the_same_run`,
  `test_send_input_is_refused_once_the_run_is_past_its_final_answer` (incl. withdraw)
- TUI: queue into a run, up arrow takes one back, unread return on stop, a background tab keeps
  its own conversation, `/close`, title sync, context from the model's report
- Telegram: a message during a run is queued

---

## Phase 34 — TUI refactor (split `app.py`)

**Goal.** `gateways/tui/app.py` (~1100 lines) and `widgets.py` (~950) become modules a reader
can hold in their head, with no behaviour change.

**Deliverables**
- Split by concern, for example: event routing / rendering of a run into a `Conversation`
  (`conversation.py` or on `tabs.Conversation`), session/tab management, slash commands and
  actions, sidebar/tab-bar refresh; `widgets.py` split into rows/cards, chrome (sidebar, welcome,
  help) and modal screens. `app.py` stays the composition of those and the bindings.
- No new abstraction layers: plain modules and functions; the public surface (`TuiApp`,
  `TuiCommands`, the widget classes tests import) keeps working or tests are updated with it.
- Update `docs/design/tui.md` for the new module map.

**Acceptance**
- `make check` green with the TUI tests unchanged in what they assert (imports may move).
- No module in `gateways/tui/` over ~450 lines; a screenshot of the TUI (two tabs, a running
  tool card, queued message, sidebar) looks identical before and after.

---

## Phase 35 — Tool timeouts, configurable and proven ✅

**Goal.** No tool call can hang a run. A per-call timeout existed (`AgentLimits.tool_timeout_seconds`,
300 s, enforced in `ToolExecutor`; the result was already an error `ToolMessage` and the run went
on; `run_command` already killed its process group on cancellation) but was not configurable,
lost the output so far, and was not proven for the tools that can actually hang.

**Deliverables**
- `AGENT_STACK_TOOL_TIMEOUT` (seconds, default 300; `off`/`0` removes the cap) in `AppConfig`,
  wired by `bootstrap` into the assistant's and the explorer's `AgentLimits`
  (`tool_timeout_seconds: float | None`); `.env.example` documents it.
- `ToolExecutor` runs every call with an output sink that keeps what a streaming tool emitted
  (bounded), so the cut-off result reads "Error: tool 'run_command' timed out after 3s; output so
  far: ..." and the run continues.
- `run_command`'s own `timeout` argument stays; a longer value is cut off at the cap (the executor
  cancels the call and `run_command` kills the whole process group, SIGTERM then SIGKILL). The tool
  docstring tells the model.
- One per-tool override: a tool's LangChain `metadata["timeout_seconds"]` replaces the cap. Only
  `delegate` sets it (`None`): a subagent run is bounded by its own limits and the parent's run
  deadline, and the 300 s cap would cut a long but healthy subagent off mid-run.

**Acceptance**
- `tests/unit/test_tool_timeouts.py`: a tool sleeping past the cap gives the timeout result and the
  fake-model run reaches its final answer; the timeout result carries the output so far;
  `run_command` with a backgrounded `sleep 999` and `timeout=999` under a 1 s cap is cut off and
  the sleep's pid is gone (process group killed); `off`/`0` parse to no cap and no cap means no
  cut-off; the metadata override; config validation.
- Smoke (`tmp/smoke_tests/P35-tool-timeouts-smoke.py`): real app with `AGENT_STACK_TOOL_TIMEOUT=3`,
  `run_command` `sleep 31` stops at 3.0 s with output so far, no `sleep` left; a real model run
  continues past the timeout to a final answer.

---

## Phase 36 — Auto-approval with structured output ✅

**Goal.** The auto-mode classifier's answer is parsed by LangChain's `with_structured_output`
into a typed verdict, not fished out of free text with a regex.

**Deliverables**
- `approval/auto.py`: a Pydantic (or TypedDict) schema `{verdict: allow|deny|ask, reason: str}`;
  the classifier calls `model.with_structured_output(schema)`; `parse_verdict` and its regex go.
- A model that cannot do structured output (no tool calling / JSON mode) or returns something
  invalid gives `ask` (the human decides), never `allow`.
- The rule layer before the classifier is unchanged.

**Acceptance**
- tests with the fake model: allow / deny / ask round-trip; an invalid or failing structured call
  becomes `ask`; a real-LLM smoke (`@pytest.mark.llm` or `tmp/smoke_tests`) against the small model.

**Done:** `approval/auto.py` has a Pydantic `_Decision {verdict: Literal["allow","deny","ask"],
reason: str}`; the classifier calls `model.with_structured_output(_Decision,
method="function_calling")` under a 30 s `asyncio.wait_for`. Any exception (no structured-output
support, timeout, provider error, a verdict outside the three, a missing field) or a non-`_Decision`
result (no tool call came back) gives `ask`; `parse_verdict`, `_text` and the regex are gone, and
the prompt no longer asks for JSON. Method: `function_calling` (ChatOpenRouter's default; both it
and `json_schema` returned valid verdicts on `typesafe/jev-router` and `z-ai/glm-5.3-flash`;
`json_mode` is not an OpenRouter method). Rules unchanged. Tests: `tests/fakes.py`
`FakeToolCallingModel`, a real `BaseChatModel` with scripted replies, so LangChain's own
`with_structured_output` parsing runs; `tests/unit/test_auto_mode.py` covers the allow/deny/ask
round-trip, invalid / text-only / raising / non-tool-calling / slow classifiers → `ask`. No
`@pytest.mark.llm` test (nothing deselects that marker, so it would run in `make check`); the
real-model check is `tmp/smoke_tests/P36-structured-approval-smoke.py`.

---

## Phase 37 — Prompt caching ✅

**Goal.** Repeated prefixes (system prompt, tool schemas, earlier turns) are served from the
provider's prompt cache, and the cache reads show in usage and cost.

**Built** ([ADR 017](decisions/017-prompt-cache-breakpoints.md))
- `models/caching.py`: `add_cache_breakpoints` puts `cache_control: {"type": "ephemeral"}` on the
  system message (caches tools + system) and on the last message with text (rolling): 2 of
  Anthropic's 4. It copies the two messages it marks; stored messages are never touched.
  `with_prompt_cache(model, name)` puts it in front of the tool-bound model as a
  `RunnableLambda`; `AgentFactory.create` applies it after `bind_tools`, so the loop call and the
  compaction call both get it and the runtime stays provider-neutral.
- Breakpoints only for `anthropic:*`, `openrouter:anthropic/*`, `openrouter:qwen/*` (explicit-only
  caches). OpenAI, DeepSeek, Grok, Moonshot, Groq, Z.AI cache automatically; Gemini 2.5+ is left
  to its free implicit cache (an explicit rolling breakpoint would pay a write every turn).
- `ChatOpenRouter` already maps `prompt_tokens_details.cached_tokens` / `cache_write_tokens` to
  `input_token_details.cache_read` / `cache_creation`; `usage_data` now also records
  `cache_write_tokens`. `SessionUsage` totals `cache_read_tokens` / `cache_write_tokens` (Telegram's
  per-run delta too) and `usage_summary` shows `12.2k in (6.1k cached)`.
- langchain-anthropic is not installed, so `anthropic:` models are untested live; the same
  content-block `cache_control` is its native format.

**Acceptance**
- `tests/unit/test_prompt_caching.py`: breakpoints on system + last text message, at most two,
  list content marks only its last text block, inputs never mutated, only explicit-cache providers
  wrapped; `tests/integration/test_usage.py`: cache reads/writes totalled and in the cost line.
- Smoke (two calls in a row, real prompt + tools ~6k tokens): Claude Haiku 4.5 via OpenRouter
  read 6104 cached tokens on call 2 (cost $0.0077 -> $0.00065); default DeepSeek V4 Pro read 5376
  automatically ($0.0047 -> $0.0011); a tool-loop run and Qwen 3.8 Flash cache too.

---

## Phase 38 — `web_screenshot` tool (Playwright) ✅

**Goal.** For web work the agent can look at a page it is building.

**Deliverables**
- `tools/builtins/screenshot.py`: `web_screenshot(url, width?, height?, full_page?)` with
  Playwright (Chromium, headless). The PNG is saved as an artifact; the tool result carries the
  path and, where the model accepts images, the image itself (coordinate with P44; until then,
  the path and page title/console errors as text).
- Same URL safety as `web_fetch` (no cloud metadata / link-local), but `localhost` allowed (the
  dev server is the point). Gated like other networked tools.
- Playwright as an optional extra (`uv sync --extra browser` + `playwright install chromium`);
  the tool is left out with a clear message when it is not installed.

**Acceptance**
- test against a local `http.server` page: a PNG artifact exists with the right size; console
  errors are reported; a blocked URL is refused.

**Built.** `tools/builtins/screenshot.py`: `screenshot_tools(artifacts)` returns `()` (one info
log line with the setup command) when Playwright does not import. Each call launches headless
Chromium (`service_workers="block"`), waits for `load` then up to 5 s for `networkidle`, and is
capped at 60 s. The PNG goes to the `LocalArtifactStore`; the text result has `Saved: <path>`
(the P44 seam), URL, status, title, viewport and up to 20 console/page errors. URL rule:
`check_url(..., allow=_allowed)` (web.py's check with a pluggable address predicate): loopback
and private LAN allowed; link-local, multicast, unspecified, reserved and two non-link-local
metadata IPs refused. A context route aborts every disallowed request the page makes; Playwright
routes never see redirect hops, so those are recorded from `request` events and checked after the
load, and the call returns an error with nothing saved if any hop was blocked (the hop request
itself has already been sent). `web_screenshot` is in the default `approval_tools`. Extra:
`browser = ["playwright>=1.65.0"]` (1.65.0 uses chromium-1254). Tests skip without a browser.

---

## Phase 39 — Multi-turn rewind in the web UI ✅

**Goal.** In the web UI you rewind from the conversation itself, to any earlier turn, as in the
TUI's `esc esc` picker: every earlier user message has a rewind action (conversation, code or
both), and the rewound message comes back into the composer to edit.

**Deliverables**
- `web/`: a rewind control on each past user message in the chat (and keyboard: `esc esc` opens
  the picker at the latest turn); turns across the fork lineage (`/sessions/{id}/turns`), the
  existing `/rewind` endpoint; file-conflict notice as today.
- `HistoryPanel`'s rewind tab stays as the list view.

**Acceptance**
- `npm test` for the turn ↔ message mapping; a smoke against the real server: rewind 3 turns
  back, both modes, the composer holds the old prompt and the files are restored.

**Built.** Every past user message in the chat (across the fork lineage) has a "rewind" action
in its bubble header (shown on hover or keyboard focus, always on touch screens) that opens the
three modes inline under the message (`web/components/RewindMenu.tsx`, the same `MODES` as the
History panel). `esc esc` on an empty composer opens it at the latest turn; Up / Down move to
the message before / after, Esc closes. The rewind reuses the panel's path: the conflict notice,
and the rewound prompt back in the composer. Hidden while a run is going. Messages map to turns
in `web/lib/rewind.ts` (`rewindTargets`): history rows and `/turns` are walked backwards and
paired by text (skill block stripped as the server does), so a message queued mid-run (no turn)
and compacted turns (no row) are skipped; no server change. `HistoryPanel`'s list is unchanged.

---

## Phase 40 — Rate limiting on the gateways ✅

**Goal.** A buggy or hostile client cannot hammer the API or the model bill.

**Deliverables**
- `gateways/ratelimit.py`: `RateLimiter(rate, per=60, clock)`, one in-process token bucket per
  key (holds `rate` tokens, earns `rate` back per minute); `take(key)` returns 0 or the seconds
  until a token is back; `rate=None` is off. No new infrastructure.
- `AppConfig.rate_messages` (`AGENT_STACK_RATE_MESSAGES`, default 30/min) for model-spending
  actions and `rate_requests` (`AGENT_STACK_RATE_REQUESTS`, default 600/min) for everything else;
  `off`/`0` disables. The gateways read them from `application.config` (no bootstrap change).
- Web: a limit dependency chained on the auth check (a wrong token never drains the owner's
  bucket), keyed by principal. Strict bucket: send message, queue input, compact, rewind, branch;
  every other authed route the loose one. Excess: `429`, `Retry-After: <s>`, detail "Too many
  requests: try again in Ns." `/health` and the SSE `/runs/{id}/events` are not limited (the API
  serves no static files). `create_app(..., clock=)` for tests. `web/lib/api.ts` reads a 429
  without detail as a sentence.
- Telegram: per chat, after the owner check; text messages (commands too) take from the messages
  bucket, button presses from the requests bucket. Excess: "Slow down: try again in Ns." at most
  once per chat per 60 s window, the update otherwise ignored.

**Acceptance**
- `tests/unit/test_ratelimit.py` (burst, refill, separate keys, off, config parsing),
  `tests/integration/test_web_rate_limits.py` (429 + Retry-After, recovery on a fake clock, 401
  spends nothing, spending calls on their own bucket, SSE unlimited, off),
  `tests/unit/test_telegram_gateway.py` (one refusal per window, recovery, per-chat buckets,
  buttons, off).
- Smoke (`tmp/smoke_tests/P40-rate-limits-smoke.py`): the real `agent-stack web` with 5/1 limits:
  `[200 x5, 429 x3]`, `Retry-After: 12`, 200 again after waiting; a second real message 429 with
  `Retry-After: 60`; the event stream still served while the general bucket is empty.

---

## Phase 41 — Export a conversation (`/export`) ✅

**Goal.** `/export md` or `/export json` writes the conversation to a file.

**Deliverables**
- `Application.export_session(session_id, fmt)` → text: Markdown (title, messages, tool calls
  with arguments and results folded) or JSON (the stored messages, redacted as the transcript is).
- `/export [md|json] [path]` in the TUI (writes to the workspace, default
  `<title>-<date>.md`), web (download), Telegram (sends the file), line REPL.

**Acceptance**
- unit tests for both formats (round-trip of the JSON, Markdown contains every user message and
  tool call); gateway tests for each `/export`.

**Built.** `app/export.py` (pure: `render`, `scrub`, `export_filename`, `free_path`,
`parse_export_args`) behind three `Application` methods: `export_session(id, fmt)` -> text,
`export_filename(id, fmt)` -> `<slug-of-title>-<YYYY-MM-DD>.<fmt>`, and `save_export(id, fmt,
path=None)` -> the written path (workspace-relative; a directory gets the default name; `-2`,
`-3`... instead of overwriting). Rows are the transcript lines (`seq, ts, run_id, kind, content,
tool_calls, tool_call_id`; `event` lines and `data` left out); every string goes through the
log redactor (`redact(text, env_secrets())`) and string values under `key`/`token`/`secret`/
`password` keys become `[redacted]`, so the transcript, which keeps raw user and model text,
is never exported raw. JSON: `{"session": {id, title, agent, created_at, updated_at, branch_of,
exported_at}, "messages": rows}`. Markdown: title, export/start date line, `## You` / `## Agent`
with times, each tool call as its JSON arguments plus the result in `<details>` (cut at 4000
characters with a count), a compaction as a marked blockquote without its file copies, skill
bodies dropped from user text. Gateways: TUI builtin (works during a run), line REPL branch,
web `GET /sessions/{id}/export?fmt=md|json` (attachment; `Content-Disposition` exposed via
CORS) + composer `/export [md|json]` download, Telegram `/export [md|json]` via
`TelegramApi.send_document` (multipart `sendDocument`; `call` takes `files`).

---

## Phase 42 — OpenTelemetry tracing ✅

**Goal.** A run can be followed in any OTLP backend: one trace per run, spans for model calls,
tool calls and subagents.

**Deliverables**
- Optional extra (`opentelemetry-sdk`, OTLP exporter); off unless `OTEL_EXPORTER_OTLP_ENDPOINT`
  (or `AGENT_STACK_OTEL=1`) is set. Spans: `run` (session, agent, model), `model.call` (tokens,
  cost, cache reads), `tool.call` (name, ok, duration; arguments redacted as in the event log),
  `subagent` as a child of the parent's tool span. Built from the existing event bus where
  possible, so the runtime does not grow tracing code.

**Acceptance**
- tests with the in-memory span exporter: span tree for a run with a tool call and a subagent;
  nothing exported or imported when off; redaction holds.

**Built.** `observability/tracing.py`: `RunTracer`, a bus sink beside the audit log and metrics
(`FanoutSink` in `bootstrap.py`), so the runtime has no tracing code. Extra `otel`
(`opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`); `tracing_from_env(os.environ)`
returns no sink (and imports nothing) unless `OTEL_EXPORTER_OTLP_ENDPOINT`,
`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` or `AGENT_STACK_OTEL=1` is set; extra missing → a warning.
Standard `OTEL_*` variables apply; service name defaults to `agent-stack`;
`OTEL_TRACES_EXPORTER=console` prints spans. Span times come from event timestamps:
- `run` (root, fresh trace): `session.id`, `agent_stack.run_id`, `gen_ai.agent.name`,
  `gen_ai.request.model` (from the first model call).
- `model.call`: `gen_ai.usage.input_tokens`/`output_tokens`, cache read/write and reasoning
  tokens, `agent_stack.cost_usd`, model. `MODEL_USAGE` arrives after the call, so the span starts
  at the previous event of the same run (run start, last tool result, ...). A `purpose` call (the
  title, made beside the run) is a zero-length span.
- `tool.call`: name, call id, arguments sanitised by `audit_record` (the audit/event-log rules),
  ok, `duration_ms`; ERROR status with the redacted error. A parallel batch's tools end together
  (the runtime publishes `TOOL_COMPLETED` after the batch).
- `subagent`: child run under the parent's `delegate` span (bound at `SUBAGENT_STARTED`, oldest
  open unbound delegate first).
- Failure ends the open spans with ERROR. A hard cancel publishes no terminal event (by design),
  so the spans end on the child's `SUBAGENT_COMPLETED`, the session's next run, the parent's end,
  or `close()` at exit, at the run's last seen event, with ERROR.

Tests: `tests/unit/test_tracing.py` (skipped without the extra). Smoke:
`tmp/smoke_tests/P42-otel-smoke.py` (local OTLP/HTTP receiver, real run with a subagent and tools).

---

## Phase 43 — MCP client ✅

**Goal.** Tools from MCP servers are tools of the agent. Decision record: ADR 018.

**Deliverables**
- `tools/mcp.py`: config in the Claude Code shape (`{"mcpServers": {name: {command, args, env} |
  {url, headers}}}`, optional `"trusted": true`) from `~/.agent-stack/mcp.json` then
  `<workspace>/.mcp.json` (workspace wins per name); `${VAR}` expanded from the environment
  (bootstrap passes it in). Official `mcp` SDK 2.x (`mcp.Client`) as a regular dependency, stdio and
  streamable HTTP; `langchain-mcp-adapters` not used (a second dependency for ~30 lines).
- `McpManager`: starts every server at once, each in its own holder task, bounded by 30 s; a server
  that fails to start or list tools is logged and reported (`failed: <reason>`), never fatal. stdio
  stderr goes to `.agent/logs/mcp-<name>.log`. Tools are `StructuredTool`s named
  `mcp__<server>__<tool>` with the server's JSON schema; text results, `is_error` -> error result;
  the P35 per-call timeout applies.
- Lifecycle: `build_application` only reads config; `Application.start_mcp()` (once, shared) runs
  on the first message or `/mcp`, and the tools join the main agent by rebuilding its runtime (the
  `/model` path; subagents do not get them). `drain_background()` closes the servers; loop teardown
  ends them otherwise (checked: no orphan process).
- Approval: `ToolPolicy.dynamic` gates `mcp__*` tools of untrusted servers; in auto mode no rule
  matches them, so the classifier decides.
- `/mcp` in the TUI (`commands.py`, a notification) and Telegram; `GET /mcp` (JSON) on the web API.

**Acceptance**
- `tests/integration/test_mcp.py`: a stdio `MCPServer` written in the test (run via
  `sys.executable`): `mcp__test__add` is bound, called with arguments through the executor and its
  result reaches the fake model as a ToolMessage; a broken server is `failed` and the run still
  works; servers `stopped` after `drain_background`; untrusted tools need approval and go to the
  classifier, trusted ones do not; `${VAR}` expansion; workspace config overrides home.
- Smoke (`tmp/smoke_tests/P43-mcp-smoke.py`): real bootstrap with `uvx mcp-server-time` and a broken
  server; one real run on `openrouter:z-ai/glm-5.3-flash` calls `mcp__time__get_current_time` through
  the approval gate and answers from its result; no server process left.

---

## Phase 44 — Images in (multimodal input) ✅

**Goal.** A user can give the agent images, at least: the model sees them as image content.
Decision record: ADR 019.

**Deliverables**
- Block shape, verified by real calls: an OpenAI-style `{"type": "image_url", "image_url":
  {"url": "data:<mime>;base64,..."}}`. ChatOpenRouter converts LangChain v1 `image` blocks for user
  messages only and sends tool content raw, so the v1 block is not seen in a ToolMessage (Haiku:
  "can't see the image"); `image_url` works in both on Claude Haiku and Gemini Flash.
- `core/images.py` (a leaf): `Image(data, mime, name)`, type sniffed from magic bytes
  (PNG/JPEG/WebP/GIF), 5 MB cap (Anthropic's limit), no downscaling and no Pillow (providers
  downscale themselves); `content_text`, `content_chars` (an image counts 1600 tokens in the
  estimate, never its base64), `without_images`.
- `MessageEnvelope.images` -> `send_message` -> `RunRequest.images` -> the runtime builds
  `HumanMessage([*image blocks, text])` (a plain string when there are none).
- Storage: images saved once as `artifacts/<sha256[:16]>.<ext>`, the transcript line keeps the
  text and `data.images`; reading turns them back into blocks (later turns, restarts). `.md`,
  export, chat search and history rows show `[image: <name>]`. A branch copies its images.
  `HistoryItem.images`; `Application.session_image()`.
- Tool results: the executor keeps image blocks of a list result (text parts still redacted and
  capped). `read_file` on an image returns it; `web_screenshot` returns the PNG plus its text
  (text only above 5 MB); MCP image content becomes image blocks instead of `[image]`.
- No vision: a model call that fails with an error about images while the input has images is
  retried once with each image replaced by `[image: <name>] (not shown: this model does not
  accept images; switch to one that does with /model)`; the model tells the user, the stored
  conversation keeps its images.
- Gateways: `image_paths()` in `gateways/base.py` (`@path` or a pasted absolute path to an
  existing image). TUI: chip under the prompt, `/image <path>`, `[image: name]` in the user row.
  Line REPL: `@path`. Web: `POST /sessions/{id}/messages` takes `images: [{name, data}]` (base64
  JSON, max 8), `GET /sessions/{id}/images/{name}` serves a stored image (auth, ownership, strict
  name), history rows carry `images`; the Composer has an Image button, paste and drag-and-drop
  with removable thumbnails; user bubbles show thumbnails (fetched with the token). Telegram:
  photos (largest size) and image documents via `getFile`, the caption is the text.
- Queued input (`send_input`) stays text only: a message with an image during a run is refused
  with a note (TUI, web, Telegram).

**Acceptance**
- `tests/integration/test_images.py`: the model receives an image block; transcript.jsonl has no
  base64 and names the artifact; a fresh store replays the same blocks and the next turn sends
  them; export (md, json), search and the event log carry the placeholder; a branch keeps its
  images; a text-only model gets one retry with placeholders; tool-result images round-trip; the
  web upload (bad data 400, empty 422), history `images` and the image route (404 for other
  names, 401 without the token).
- `tests/unit/test_images.py`: type sniffing and the cap, token estimate, `image_paths`,
  `read_file` on a PNG, MCP image blocks, TUI chip + send + `/image`, line REPL `@path`, Telegram
  photo and document with caption, refused bad image and image during a run.
- Smoke (`tmp/smoke_tests/P44-images-smoke.py`, `P44-images-ui.py`): a PNG with "BANANA" on red
  described correctly by `openrouter:anthropic/claude-haiku-5.5`, recalled after a restart; the
  default `deepseek-v4-pro` (no vision) answers that it cannot see images and to switch with
  `/model`; `web_screenshot` then "what colour is the heading" answered green; `read_file` on the
  PNG read "BANANA"; the web UI uploads, shows the thumbnail before and after a reload, and takes
  a pasted image.

---

## Phase 45 — Token streaming ✅

**Goal.** A reply appears as the model writes it: the web UI and the TUI grow the assistant
message (and its reasoning) from streamed tokens instead of waiting for the whole step.

**Deliverables**
- Runtime calls the model with `astream` and aggregates the chunks into the same `AIMessage`
  `ainvoke` returns (content, tool calls, usage incl. cache tokens and cost, reasoning). While it
  streams it publishes `assistant.delta` (`text`, `kind` `text|reasoning`, `step`), coalesced
  (~50 ms / 64 chars). `assistant.message`, `model.reasoning` and `model.usage` stay exactly as
  before. A model that cannot stream falls back to `ainvoke` once and is remembered. Deadline and
  cancellation interrupt a stream. Compaction does not stream.
- Deltas are ephemeral: not sequenced, not in the durable event log, not in the audit log,
  metrics or tracing; SSE resume by sequence is unaffected (a missed partial is fine, the final
  message replays).
- Web: the SSE route forwards live deltas (without an `id:`); the reducer grows the current
  assistant/reasoning item and the final message replaces it. TUI streams into the current
  `AgentMessage`. Telegram and the line REPL stay on final messages. Child-run deltas never
  render in the parent conversation.

**Acceptance**
- tests: deltas in order and coalesced; aggregated message equals the chunks (tool calls,
  usage); deltas absent from the event log, audit and sinks; SSE replay after a reconnect has
  the final message and no deltas; cancellation mid-stream; non-streaming fallback; web reducer
  delta to final replacement and child deltas ignored.
- Smoke with the real default model through the web gateway: deltas counted, time to first
  delta vs full reply, final text equals the concatenated deltas, tool call and usage still work;
  screenshots mid-stream.

**Built.** `AgentRuntime._stream_model` (behind the existing `_invoker`, inside `_within`): sums
the `astream` chunks and returns `message_chunk_to_message(whole)`; a `_Coalescer` publishes
`assistant.delta` on a kind switch, at 64 chars or 50 ms (the first piece at once), and at the end;
the cancel token is checked per chunk. `AttributeError`/`NotImplementedError` before the first
chunk switches the runtime to `ainvoke` for good; no bus wired means `ainvoke` too. Reasoning
deltas come from `additional_kwargs["reasoning_content"]` (OpenRouter/DeepSeek style); reasoning
only in content blocks shows when the step ends, as before. Ephemeral events: `EPHEMERAL_EVENTS`
in `core/events.py`; `InMemoryEventBus.publish` hands them to current subscribers with
`sequence` -1 and returns before sequencing, `_persist` (event log) and the audit sink
(`FanoutSink`: audit, logger, metrics, tracing). Web: `_stream` runs a bus subscription beside
the log poll and spends each poll pause forwarding deltas (redacted with the event-log
sanitiser, no `id:` line). `web/lib/events.ts` grows `delta:<run>:<step>:<kind>` items (plain
text while streaming, Markdown on the final), the final event replaces them in place, a delta
for a finished step or a child run is dropped, and a final the event log capped (2000 bytes)
keeps the streamed text. TUI: `AgentMessage.grow` (queued `Markdown.append`, never before the
mount), `settle` on the final; the thinking card is mounted above the streamed reply. Telegram
and the line REPL ignore deltas. Tests: `tests/unit/test_streaming.py`, web gateway SSE test,
TUI tests, `web/lib/events.test.ts`. Smoke: `tmp/smoke_tests/P45-token-streaming-smoke.py`.

## Phase 46 — Subagent conversations stored inside their chat ✅

**Goal.** Every subagent run can be audited later from the chat that started it: its own
conversation and a record of its ids, task and outcome, in the chat's folder.

**Deliverables**
- A child session id is `<parent session id>::<agent>::<id>` (was `<parent run id>::...`), and
  `FileConversationStore.session_dir` maps it to `<chat>/subagents/<agent>-<id>/` (a subagent of
  a subagent nests one level deeper). Deleting the chat deletes them. Older folders named by the
  parent run id are still found where they are.
- `subagent.json` beside the child transcript: agent, run id, parent run id, session id, depth,
  task, status, steps, tool calls, error, `updated_at`. Written when the child starts and again
  when it ends (`RunManager(subagent_record=...)`, wired in bootstrap).
- A failed or cancelled run reports the steps and tool calls it made (they were 0), so the
  parent's `delegate` summary, `subagent.completed` and the runs table are right.

**Acceptance**
- tests: a child that hits its tool-call limit is stored under `<chat>/subagents/`, its record
  has the run ids, task, `failed`, the error and real counts, and the parent's summary says how
  far it got; an older flat folder still resolves; nested subagents nest.
- Smoke with the real default model: a delegation in a fresh workspace lands in
  `<chat>/subagents/explorer-<id>/` with `subagent.json`, `transcript.jsonl`, `transcript.md`.

**Built.** `core/run.py` (`start_subagent` prefixes the parent run's session id,
`_record_subagent`), `core/runtime.py` (counters on failure and hard cancel),
`persistence/conversation.py` (`session_dir`, `write_subagent_record`), `app/bootstrap.py`.
Tests: `tests/unit/test_subagents.py`. Smoke: `tmp/smoke_tests/P46-subagent-folders.py`.

## Phase 47 — Web subagent panel ✅

**Goal.** In the web UI a subagent's work is one click away and out of the main conversation:
its card opens a side panel that shows what it is doing, live, and what it did, later.

**Deliverables**
- `subagent.started` carries the task. The subagent card shows the task and an Open button.
- `SubagentPanel` (right side, where the files panel goes; one at a time): status, tool count, the
  task, and the subagent's tool cards, reasoning, streamed and final replies, failure, and its own
  subagents as cards that open in the panel. It follows the child run with its own `useLive`, so
  the server replays the stored events and then goes live: the same view for a running and a
  finished subagent.
- The conversation no longer shows subagent events inline (only the card). A subagent call that
  needs approval opens its panel; `y`/`n` still answer it.
- A reopened conversation: a stored `delegate` call gets "Open what the subagent did" (the run id
  comes from its summary).
- Reducer: `LiveState.root` is the followed run, so a child run followed as the root gets its
  phase, deltas and failure, and its own children nest under it.

**Acceptance**
- web test: following a child run as the root (deltas, depth, task, failure, phase).
- Smoke with the real default model and the real web UI (Playwright): live panel with the child's
  tool call and reply, none of it in the conversation, done state; after a reload the stored
  delegate call opens the same panel with its tool calls and task.

**Built.** `web/components/SubagentPanel.tsx`, `web/lib/events.ts` (`root`, `isRoot`,
`depthOf`, subagent `task`), `web/lib/useLive.ts` (`follow` sets the root), `web/app/page.tsx`
(panel slot, `delegatedRun`, depth-0 filter, approval auto-open), `web/components/Cards.tsx`
(`SubagentCard` task and Open), `core/run.py` (task on `subagent.started`). Tests:
`web/lib/events.test.ts`. Smoke: `tmp/smoke_tests/P47-subagent-panel-ui.py`.

## Phase 48 — No step or tool-call cap; subagents report before their time runs out; TUI subagent screen ✅

**Goal.** A run is never cut off for making "too many" calls, a subagent that runs long still hands
back what it found, and the TUI can open a subagent like the web UI.

**Deliverables**
- `AgentLimits.max_steps` / `max_tool_calls`, `StepLimitExceeded` / `ToolCallLimitExceeded` and
  `AGENT_STACK_MAX_STEPS` are gone. A run is bounded by its time limit
  (`AGENT_STACK_MAX_RUNTIME`), the conversation budget and cancellation.
- The explorer gets half the run's time limit, so the parent can still use its report.
- At 3/4 of its time a subagent is told (before its next model call) to stop and report what it
  found, the evidence and what is unchecked. If the deadline still cuts it off (one slow call can
  span the warning), it gets one more call, with any cut-off tool calls answered "not run", of up
  to a quarter of its time (at least 60 s); the report is marked "(stopped at the time limit)" and
  its usage counted. No report: the subagent fails as before. A top-level run is never nudged.
- TUI: `SubagentRow` shows the task, is focusable and opens `SubagentScreen` (Enter or click),
  which reads the child run's events from the log and polls while it runs. A stored `delegate`
  call in a reopened conversation gets a done row that opens the same way.
- A reply the event log cut short (`...[truncated N bytes]`) shows whole once the subagent has
  finished, from its own transcript, in the TUI screen and the web panel.

**Acceptance**
- tests: the nudge reaches a subagent and not a top-level run; a subagent cut off mid-tool reports
  through the extra call, and fails when it cannot; the P46 audit test with a deadline; TUI row to
  screen (tool card, reply, done state, Esc) and the stored delegate row; limits removed from the
  runtime, config, invariant and hardening tests.
- Smoke with the real default model: a subagent past the old 100-call cap finishes (129 tool
  calls); a subagent with a tight limit hands back a report.

**Built.** `core/runtime.py` (`WRAP_UP`, `WRAP_UP_AT`, `_wrap_up`, the loop's deadline handler),
`core/limits.py`, `core/errors.py`, `app/config.py`, `app/bootstrap.py` (explorer limits),
`gateways/tui/subagent.py`, `rows.py` (`SubagentRow.Open`), `runs.py`, `sessions.py`,
`styles.tcss`, `bench/harbor_agent.py`, `.env.example`. Tests: `test_subagents.py`,
`test_hardening.py`, `test_tui.py`. Smoke: `tmp/smoke_tests/P48-subagent-wrap-up.py`.

---

## Phase 49 — Tool permissions only narrow: skill `allowed-tools`, subagent inheritance, `coder` subagent ✅

**Goal.** A skill's `allowed-tools` and a subagent's tool set can only narrow what a run may do,
never widen it, so a write-capable `coder` subagent is safe to offer.

**Deliverables**
- `allowed-tools` is enforced as a restriction, never a pre-approval (ADR 020): once `load_skill`
  loads a skill that declares it, the run may call only those tools plus `load_skill` /
  `read_skill_resource`; any other call becomes an error result naming the skill and never reaches
  the approval prompt. Listed tools that need approval still need it. Several loaded skills
  intersect; the scope (`SkillScope` on `RunContext`) ends with the run. The frontmatter takes a
  space/comma string or a YAML list; an explicit empty list allows no tool.
- `delegate` starts a child as `inherit(child, parent, scope)`: only tools the parent has and its
  active skill allows, and `ToolPolicy.union` of both policies, so a call the parent would have to
  approve (by name or by a dynamic rule such as a read outside the workspace) needs approval in the
  child too. Composes down any depth. Approval mode, broker and advisor were already shared.
- A subagent's approval events also go to its top-level run's stream, with the child's run and
  session ids, so the CLI, TUI and Telegram (which read only that stream) can answer them; before,
  a child waiting for a human hung there.
- `coder` subagent (read, `write_file`, `edit_file`, `run_command`) on the main model with its own
  prompt, registered beside `explorer`; one line about it in the main prompt.

**Acceptance**
- tests: `allowed-tools` parsing (commas, list, empty); a loaded skill blocks an unlisted gated
  tool without asking while a listed one runs; scopes intersect; `inherit` cuts tools and unions
  approvals (name and dynamic); a child's write asks on the child's run and runs only when
  approved, answered from the top-level stream (hangs without the forwarding); `coder` is registered within the parent's tools on the main model.
- Smoke with a real model (ask mode, temp workspace): a delegated edit to `coder` asks for
  approval and changes the file; a skill with `allowed-tools: read_file` cannot run a command.

**Built.** `skills/loader.py`, `skills/models.py`, `skills/tools.py` (`load_skill` narrows),
`core/run_context.py` (`SkillScope`), `core/approval.py` (`ToolPolicy.union`), `core/runtime.py`
(skill scope check before approvals), `core/run.py` (approval events forwarded to the root
stream), `subagents/delegation.py` (`inherit`), `app/bootstrap.py`
(`coder`, `CODER_SYSTEM_PROMPT`). Docs: `docs/STATUS.md` known limit, `docs/design/skills-and-instructions.md`,
ADR 020. Tests: `test_tool_permissions.py`, `test_skills.py`, `test_small_model.py`. Smoke:
`tmp/smoke_tests/P49-tool-permissions.py`.

---

## Phase 50 — Storage lifecycle: delete removes what a chat owns, `agent-stack gc` the rest ✅

**Goal.** Deleting a chat removes everything it owns, and long-lived storage does not grow
without bound, without ever deleting what a kept conversation, a branch or a running process
can still reach.

**Deliverables**
- Deleting a chat also deletes its subagents' sessions: every session whose id starts with
  `<chat>::` or, before P46, `<one of its run ids>::` (their rows in every session-keyed table,
  and the old flat folders). Branches forked from it stay (each is its own conversation).
- `agent-stack gc` reports, `agent-stack gc --apply` deletes, never automatically:
  rows of sessions that have no `sessions` row; subagent sessions whose chat is neither a session
  nor a run of a kept one; workspace artifacts (`.agent/artifacts/`) no transcript and no logged
  event of a kept session mentions; change snapshot folders nothing maps to; `.tmp-*` leftovers.
  Folders the index has no row for are reported and kept (they may belong to another
  `AGENT_STACK_DB`). Anything touched in the last 24 hours is kept.
- Event-log retention: events live as long as their conversation (fork points, rewind, history
  and cost read them); deletion and the orphan sweep are the bound. No dead-branch GC: every
  branch is a listed conversation.
- The approval broker forgets a request once its run has the answer; deciding it again is
  refused as unknown.
- Rules written down in `docs/design/conversation-storage.md` section 9.

**Acceptance**
- tests with real SQLite and files in tmp dirs: delete removes P46 and pre-P46 subagent sessions
  and keeps a branch; delete-then-GC leaves no orphan artifact, snapshot or index row and keeps
  the other chat's; orphan rows and dead subagents go, a row-less folder is reported and kept; a
  dry run deletes nothing; the grace period keeps fresh files and events; the CLI dry run and
  `--apply`; the broker drops answered and cancelled requests.
- Smoke on a copy of the owner's real `.agent/` + `agent.db` (aged two days): delete a chat with
  two pre-P46 subagents, `gc` lists 12 artifacts (689 KB), `gc --apply` deletes them, a second
  `gc` finds nothing, and the app lists and opens the 20 remaining chats.

**Built.** `persistence/gc.py` (`StorageCollector`, `GcReport`), `persistence/sqlite.py`
(`SESSION_TABLES`, `SqliteSessionStore.delete` + `subagent_sessions`), `persistence/changes.py`
(`snapshot_dir`), `core/approval.py`, `app/application.py` (`storage_gc`), `app/bootstrap.py`,
`observability/cli.py` (`gc`). Tests: `tests/integration/test_storage_gc.py`,
`tests/unit/test_approval.py`. Smoke: `tmp/smoke_tests/P50-storage-gc.py`.

---

# Appendix A — Target file tree

Grow into this; do not create it all up front.

```text
src/agent_stack/
├── core/
│   ├── agent.py          # Agent, AgentLimits, SubAgentSpec
│   ├── runtime.py        # AgentRuntime
│   ├── state.py          # AgentState, TodoItem
│   ├── session.py        # Session
│   ├── run.py            # Run, RunStatus, RunManager, RunHandle
│   ├── events.py         # EventType, AgentEvent
│   ├── event_bus.py
│   ├── context.py        # ContextBuilder
│   ├── cancellation.py
│   ├── limits.py
│   ├── errors.py
│   └── factory.py        # AgentFactory
├── models/
│   ├── factory.py
│   └── configuration.py
├── tools/
│   ├── registry.py
│   ├── executor.py
│   ├── policies.py
│   └── builtins/
│       ├── terminal.py
│       ├── filesystem.py
│       ├── memory.py
│       └── todos.py
├── subagents/
│   └── delegation.py
├── persistence/
│   ├── base.py
│   ├── memory.py
│   ├── sqlite.py
│   ├── migrations.py
│   └── serializers.py
├── memory/
│   ├── models.py
│   └── store.py
├── artifacts/
│   ├── base.py
│   └── local.py
├── gateways/
│   ├── base.py
│   ├── cli.py
│   ├── web/
│   ├── tui/
│   └── telegram/
├── observability/
│   ├── logging.py
│   ├── metrics.py
│   └── tracing.py
└── app/
    ├── config.py
    ├── application.py
    └── bootstrap.py      # the ONLY composition root
```

---

# Appendix B — Domain types to place over time

`Agent`, `AgentLimits`, `SubAgentSpec`, `AgentState`, `Session`, `Run`, `AgentEvent`,
`ToolExecutionResult`, `ArtifactRef`, `MemoryItem`, `ApprovalRequest`, `MessageEnvelope`.
Do **not** create parallel message classes — use LangChain messages at the model boundary
(§§119–120).

---

# Appendix C — "Do Not Build Yet" list

Keep this visible while coding (§171):

```text
[ ] Generic middleware framework
[ ] Vector database
[ ] Automatic memory extraction
[ ] Distributed queue
[ ] Kubernetes
[ ] Multi-tenant cloud architecture
[ ] Plugin marketplace
[ ] Graph-based workflow compiler
[ ] Model router
[ ] Browser automation
[ ] Voice gateway
[ ] Mobile application
[ ] Autonomous scheduler
```

Whenever you want one, ask: **"What concrete current requirement forces this
abstraction?"** If there's no answer, defer it.

---

# Appendix D — Naming conventions (§150)

Prefer: `Agent`, `AgentRuntime`, `Session`, `Run`, `ToolRegistry`, `ToolExecutor`,
`MemoryStore`, `ArtifactStore`, `CheckpointStore`, `EventBus`, `SessionResolver`,
`ContextBuilder`, `RunManager`.
Avoid vague names: `Helper`, `Utility`, `Manager`, `Processor`, `Handler` — unless they
name a real domain concept.

---

# Appendix E — Git milestones (§172)

```text
feat: initialize project
feat: add model factory
feat: add tool registry
feat: add tool executor
feat: implement agent loop
feat: add errors and limits
feat: add deterministic runtime tests
feat: add runtime events
feat: add sessions
feat: persist sessions in sqlite
feat: add checkpoints
feat: add run manager
feat: add cancellation and session locking
feat: add filesystem tools
feat: add terminal tool
feat: add artifacts
feat: add context compaction
feat: add composition root
feat: add application service layer
feat: add subagents
feat: add memory
feat: add planning tools
feat: add cli/tui gateway
feat: add web gateway
feat: add telegram gateway
feat: add approval flow
feat: add observability
feat: harden runtime
```

---

# Appendix F — ADRs to record in `docs/decisions/`

- `001-agent-vs-session.md` — `Agent` = configuration, `Session` = durable context.
- `002-explicit-loop.md` — custom async loop instead of LangGraph first.
- `003-event-protocol.md` — gateways consume `AgentEvent`, not LangChain messages.
- `004-artifacts.md` — large outputs live outside model messages.
- `005-subagents-as-tools.md` — delegation reuses the tool execution path.
- `006-composition-root.md` — `bootstrap.py` is the only wiring point.
- `007-naming-agentconfig-vs-agent.md` — resolve the §3.3 naming decision.
