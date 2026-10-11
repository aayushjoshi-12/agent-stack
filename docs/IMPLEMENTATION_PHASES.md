# agent-stack — Phased Implementation Plan

> A single, ordered roadmap that turns the architecture design document **and** the
> composition-root guidance into a sequence of small, buildable, testable phases.

| | |
|---|---|
| **Package** | `agent_stack` (`src/agent_stack/`) |
| **Python** | 3.12+ · `uv` |
| **Style** | async-first, single process, SQLite first |
| **Created** | 2026-10-11 |
| **Companion docs** | [`design/source-design-document.md`](design/source-design-document.md) · [`decisions/`](decisions/) |

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
  0  Foundation & tooling                🟡
  1  Model layer                          🟡
  2  Tool registry                        ✅
  3  Tool executor                        🟡
  4  Agent definition (Agent/limits)      🟡
  5  AgentRuntime loop + AgentState       🟡
  6  Errors & limits                      ⬜

Milestone B — Durability & state
  7  Sessions & in-memory stores          🟡
  8  Event model & event bus              ⬜
  9  SQLite index + file transcripts      ⬜
 10  Checkpoints & tool executions        ⬜
 11  RunManager + RunHandle               ⬜
 12  Cancellation, session lock, version  ⬜

Milestone C — Real capabilities
 13  Workspace & filesystem tools         ⬜
 14  Terminal tool subsystem              ⬜
 15  Artifact store                       ⬜
 16  Context builder & compaction         ⬜
16b  Skills (SKILL.md)                    ⬜
16c  Agent instructions (AGENTS.md)       ⬜

Milestone D — Composition & application
 17  Composition root (bootstrap.py)      🟡
 18  Application service layer            ⬜
 19  Gateway contract (base.py)           ⬜

Milestone E — Multi-agent & memory
 20  Subagents (delegation)               ⬜
 21  Persistent memory                    ⬜
 22  Planning / TODOs                     ⬜

Milestone F — Gateways
 23  CLI / TUI gateway                    ⬜
 24  Web gateway (FastAPI + SSE)          ⬜
 25  Telegram gateway                     ⬜

Milestone G — Trust & operations
 26  Approval flow                        ⬜
 27  Observability & invariant tests      ⬜
 28  Hardening                             ⬜
```

> **Build order one-liner** (keep visible):
> model → messages → registry → executor → loop → errors/limits → tests →
> events → sessions → sqlite **index** + **file transcripts** → checkpoints →
> run manager → cancellation → filesystem → terminal → artifacts → context →
> **skills + AGENTS.md** → bootstrap → application → gateways →
> subagents → memory → planning → approval → observability → hardening.

---

# Milestone A — The minimal loop

**Demo at the end:** `python -m agent_stack.app.bootstrap "What files are in ./src?"`
returns a final answer after calling a real tool, with deterministic tests proving the
loop.

---

## Phase 0 — Foundation & tooling 🟡

**Goal.** A clean, importable package with a fast check loop.

**Deliverables**
- `__init__.py` in `core/`, `tools/`, `tools/builtins/`, `app/`.
- `app/config.py` — typed, frozen `AppConfig` (no scattered `os.getenv()`).
- `.env.example` (never commit real keys; the current `.env` has one — add it to
  `.gitignore` if not already).
- Confirm `make check` (lint + typecheck + test) runs green/empty.

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

## Phase 1 — Model layer 🟡

**Goal.** One place that turns a model string into a `BaseChatModel`, with capability
validation.

**Why.** Provider-specific construction must not leak into the runtime. §8–9.

**Deliverables**
- `models/factory.py` — `build_model(model: str) -> BaseChatModel`.
- `models/configuration.py` — `ModelCapabilities` + `validate_capabilities(...)`.
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

## Phase 3 — Tool executor 🟡

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

## Phase 4 — Agent definition 🟡

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

## Phase 5 — AgentRuntime loop + AgentState 🟡

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

## Phase 6 — Errors & limits ⬜

**Goal.** Classify failures; guarantee the loop terminates. §43, §93.

**Deliverables**
- `core/errors.py` — hierarchy (`AgentError`, `ModelError`, `ToolError`,
  `ToolNotFoundError`, `ToolTimeoutError`, `PersistenceError`, `RunCancelled`,
  `RunLimitExceeded`).
- `core/limits.py` — enforcement of `max_steps`, `max_tool_calls`, `max_runtime_seconds`.
- Executor: unknown tool → `ToolNotFoundError` → error `ToolMessage`; timeouts →
  `ToolTimeoutError`.

**Acceptance tests**
- `test_stops_after_max_steps`
- `test_stops_after_max_tool_calls`
- `test_stops_after_runtime_deadline`
- `test_unknown_tool_becomes_error_message`
- `test_tool_timeout_becomes_error_message`

**Defer:** retry policies (§44–45) until a real transient failure appears.

---

# Milestone B — Durability & state

**Demo at the end:** send messages, kill the process, restart, continue the same
session; crash mid-run and resume without duplicating a completed tool.

---

## Phase 7 — Sessions & in-memory stores 🟡

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

## Phase 8 — Event model & event bus ⬜

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

**Defer:** durable event log / replay from DB (Phase 10+), Redis/NATS (§33).

---

## Phase 9 — SQLite index + file-based conversation store ⬜

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

## Phase 10 — Checkpoints & tool executions ⬜

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

## Phase 11 — RunManager + RunHandle ⬜

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

## Phase 12 — Cancellation, session lock, versioning ⬜

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

## Phase 13 — Workspace & filesystem tools ⬜

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

## Phase 14 — Terminal tool subsystem ⬜

**Goal.** Async subprocess execution as a subsystem, not a one-liner. §27, §80–81.

**Deliverables**
- `tools/builtins/terminal.py` — `run_command`:
  - `asyncio.create_subprocess_exec` (no implicit `shell=True`),
  - process-group creation + termination on cancel,
  - timeout enforcement, stdout/stderr caps (e.g. 32 KB),
  - environment allowlist (`PATH`, `HOME`, `LANG`),
  - `CommandResult` with `exit_code`, `stdout`, `stderr`, `duration_ms`, `timed_out`.

**Acceptance**
- `test_command_timeout`, `test_command_env_is_filtered`, `test_command_cwd_preserved`.
- Cancellation kills child processes.

**Defer:** sandboxing/containers, approval (Phase 26).

---

## Phase 15 — Artifact store ⬜

**Goal.** Large outputs live outside model context; the model sees a reference. §25–26, §112.

**Deliverables**
- `artifacts/base.py` — `ArtifactStore` protocol + `ArtifactRef`.
- `artifacts/local.py` — `LocalArtifactStore` under `workspace/.agent/artifacts/<uuid>`.
- Executor: exceeding an output cap stores the full output as an artifact and returns a
  truncated/summarized `ToolMessage` with `artifact://<id>`.

**Acceptance test**
- `test_large_output_becomes_artifact_and_short_message`

**Defer:** S3 backend, artifact garbage collection (§148–149).

---

## Phase 16 — Context builder & compaction ⬜

**Goal.** Bound the model's context; never send unbounded history. §16, §52–54.

**Deliverables**
- `core/context.py` — `ContextBuilder.build(state, session)` and `.compact(...)`.
- v1 strategy: `recent window`; then `summary + recent messages`.
- Compaction preserves goals, decisions, files changed, failures, open questions.

**Acceptance tests**
- `test_context_builds_system_plus_recent_messages`
- `test_large_conversation_is_compacted` (fake summarizer model)

**Defer:** vector/semantic retrieval (§57), automatic memory extraction.

---

## Phase 16b — Skills (`SKILL.md`) ⬜

**Goal.** Discover and progressively load file-based skills. Full design:
[`design/skills-and-instructions.md`](design/skills-and-instructions.md).

**Deliverables**
- `skills/models.py` (`SkillMetadata`, `Skill`, `SkillResource`), `skills/loader.py`
  (discover + parse YAML frontmatter + name validation), `skills/registry.py`.
- Discovery roots: `<workspace>/.agent/skills/`, repo `skills/`, `~/.claude/skills/` (opt-in).
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

## Phase 16c — Agent instructions (`AGENTS.md`) ⬜

**Goal.** Load project instructions with nearest-file-wins precedence.

**Deliverables**
- `context/instructions.py` — `InstructionsLoader.load(target_dir, stop)` resolving every
  `AGENTS.md` (fallback `CLAUDE.md`) from the working dir up to the root, nearest first.
- `ContextBuilder` composes: base prompt → instructions (delimited) → skills metadata →
  memory → recent messages.
- Size cap (8 KB/file, 32 KB total) + `(path, mtime)` cache.

**Acceptance**
- `test_loads_root_agents_md`, `test_nested_agents_md_nearest_wins`,
  `test_supports_claude_md_fallback`, `test_instruction_size_cap_enforced`,
  `test_instructions_injected_into_system_prompt`.

**Smoke (real model):** an `AGENTS.md` states a project convention; ask a question whose
correct answer follows the convention and confirm the model obeys it.

**Defer:** monorepo per-package permissions.

---

# Milestone D — Composition & application

**Demo at the end:** one `create_application()` call wires everything and can be driven
by a trivial CLI/in-process client.

---

## Phase 17 — Composition root 🟡

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

## Phase 18 — Application service layer ⬜

**Goal.** The stable boundary every gateway talks to. §21–23, §68.

**Deliverables**
- `app/application.py` — `Application` with:
  - `send_message(envelope | (user_id, session_id, message)) -> RunHandle`
  - `cancel_run(run_id, principal_id)`, `get_session(...)`, `subscribe(run_id)`.
- `IdentityResolver` + `SessionResolver` (gateway identity → principal → session). §11, §103.

**Acceptance**
- A fake gateway can drive a full run using only `Application`; it never imports `core`.

**Defer:** authorization policy depth (start with an owner check).

---

## Phase 19 — Gateway contract ⬜

**Goal.** One inbound shape and one outbound protocol. §60–61.

**Deliverables**
- `gateways/base.py` — `MessageEnvelope`, `Gateway` protocol, rendering hints.
- Gateways convert native protocol → `MessageEnvelope` and consume `AgentEvent`.

**Acceptance**
- A test constructs an `Application`, feeds a `MessageEnvelope`, and asserts events.

**Defer:** gateway-specific rate limits (§126) until a gateway exists.

---

# Milestone E — Multi-agent & memory

---

## Phase 20 — Subagents ⬜

**Goal.** Delegation reuses the normal tool path. §46–51.

**Deliverables**
- `subagents/delegation.py` — a `delegate(agent_name, task)` tool that resolves a child
  `Agent` via `AgentFactory`, runs a child run under `parent_run_id`, and returns a
  compact `AgentResult`/summary (not the full transcript).
- Enforce `max_subagent_depth`, `max_subagents`.

**Acceptance tests**
- `test_runs_subagent`
- `test_enforces_subagent_depth`
- `test_parallel_subagents_run_concurrently`

**Defer:** specialized researcher/coder definitions until the mechanism works (§115).

---

## Phase 21 — Persistent memory ⬜

**Goal.** Cross-session facts, explicitly managed. §56–59.

**Deliverables**
- `memory/models.py` — `MemoryItem`; `memory/store.py` — `MemoryStore` protocol + SQLite
  implementation (key/value + keyword search, namespaces).
- `tools/builtins/memory.py` — `remember`, `recall`, `forget`.

**Acceptance**
- Facts persist across sessions; `recall` finds by keyword; namespaces isolate data.

**Defer:** vector embeddings, automatic extraction (§56, §171).

---

## Phase 22 — Planning / TODOs ⬜

**Goal.** Small explicit task list the model can manage. §55.

**Deliverables**
- `core/state.py` — add `TodoItem`/`TodoStatus`.
- `tools/builtins/todos.py` — `todo_list`, `todo_add`, `todo_complete`.

**Acceptance**
- Todos survive a checkpoint and resume.

**Defer:** sophisticated planners, DAG workflows.

---

# Milestone F — Gateways

---

## Phase 23 — CLI / TUI gateway ⬜

**Goal.** Prove the whole stack locally with the least protocol friction. §66.

**Deliverables**
- `gateways/cli.py` (start here, simplest) then `gateways/tui/` (Textual, §143).
- Renders `AgentEvent`s; never touches tools/models/stores.

**Acceptance**
- Interactive session works; `Ctrl+C` maps to `Application.cancel_run`.

**Defer:** rich execution trees until events are stable.

---

## Phase 24 — Web gateway ⬜

**Goal.** HTTP commands + SSE event stream. §62–63.

**Deliverables**
- `gateways/web/app.py` — FastAPI routes:
  `POST /sessions`, `GET /sessions`, `POST /sessions/{id}/messages`,
  `GET /sessions/{id}`, `GET /runs/{id}/events` (SSE), `POST /runs/{id}/cancel`.
- Add `fastapi`/`uvicorn` to dependencies only now.

**Acceptance**
- `send_message` returns `run_id`; SSE replays from `last_event_sequence` (§104).

**Defer:** WebSocket approvals (until Phase 26).

---

## Phase 25 — Telegram gateway ⬜

**Goal.** Adapter only; quirks stay at the edge. §64–65.

**Deliverables**
- `gateways/telegram/` — update → `MessageEnvelope` → `Application`; debounced status
  rendering; `/new`, `/sessions`, `/cancel` commands (§146).
- External-message idempotency key mapping (§105).

**Acceptance**
- Same `session_id` continues a conversation started in the Web/TUI gateway.

**Defer:** inline-keyboard approvals until Phase 26.

---

# Milestone G — Trust & operations

---

## Phase 26 — Approval flow ⬜

**Goal.** Human-in-the-loop for dangerous tools, gateway-agnostic. §78–79, §124–125.

**Deliverables**
- `core/approval.py` — `ApprovalRequest`, `ApprovalStatus`.
- `ToolPolicy.requires_approval`; run enters `WAITING_APPROVAL`; emits
  `approval.required`; `Application.resume` executes after approval.
- Validate `approval_id + run_id + session_id + principal_id` binding (§125).

**Acceptance tests**
- `test_requires_approval_pauses_run`
- `test_approval_resumes_and_executes`
- `test_rejected_approval_returns_error_tool_message`
- `test_approval_cannot_be_replayed_against_another_run`

**Defer:** command classification beyond a simple flag.

---

## Phase 27 — Observability & invariant tests ⬜

**Goal.** Make the system debuggable and lock in the invariants. §87–89, §92.

**Deliverables**
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

**Defer:** OpenTelemetry exporters, dashboards.

---

## Phase 28 — Hardening ⬜

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
[ ] Skill system
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
