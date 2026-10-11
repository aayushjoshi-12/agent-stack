# agent-stack — Test Plan

> Coverage strategy for what exists and what is coming. The runtime must be testable
> **without real LLM calls**; real-model tests are opt-in.

---

## 1. Layers

| Layer | What | Location | Runs in CI? |
|---|---|---|---|
| **Unit** | registry, executor, state, limits, serializers, session resolution | `tests/unit/` | yes |
| **Contract** | one suite run against every implementation of a store protocol (in-memory + SQLite) | `tests/contract/` | yes |
| **Invariant** | the runtime guarantees in §92 | `tests/invariants/` | yes |
| **Integration** | real SQLite, real filesystem, real subprocess (fake model) | `tests/integration/` | yes |
| **LLM** | real provider call | `tests/integration/` (`@pytest.mark.llm`) | opt-in |
| **E2E** | gateway → application → runtime → store → events | `tests/e2e/` | yes (fake model) |
| **Smoke** | real deps, nothing mocked on the changed path (incl. real LLM) | `tmp/smoke_tests/` | **never** |

---

## 2. The deterministic fake model

Everything hinges on this. `tests/fakes.py` provides `FakeChatModel`, scripted with a queue
of responses:

```python
model = FakeChatModel([
    ai(tool_calls=[call("read_file", {"path": "a.txt"})]),
    ai(content="Both files exist."),
])
```

- Implements the minimal `BaseChatModel` surface the runtime uses (`ainvoke`, `bind_tools`,
  and — later — `astream`).
- `bind_tools(...)` returns itself (records the bound tools).
- Records every `ainvoke` call so tests can assert context.
- Raises a clear error if asked for more responses than scripted (guards infinite loops).

Also: `SleepTool(seconds)` for the parallelism proof (§138), and `FailingTool`.

---

## 3. Coverage — what exists now vs. what's missing

| Module | Tests today | Missing (this build) |
|---|---|---|
| `tools/registry.py` | none | duplicate, unknown, `get`, `all` |
| `tools/executor.py` | none | single, parallel, ordering, tool error, **args bug** |
| `core/runtime.py` | none | direct response, tool round-trip, persistence, **no-duplication** |
| `core/agent.py` | none | immutability, defaults |
| `core/session.py` | none | copy-on-read, isolation |
| `models/` | none | factory returns model; capability validation |
| **`tests/conftest.py`** | none | tmp workspace, fake model, sample tools |

---

## 4. Coverage — coming next (per phase)

- **P03/P05** — the loop tests listed in §137 of the design doc:
  `test_returns_direct_model_response`, `test_executes_single_tool_call`,
  `test_executes_parallel_tool_calls`, `test_preserves_tool_call_ids`,
  `test_continues_after_tool_error`, `test_persists_messages_without_duplication`.
- **P06** — `test_stops_after_max_steps`, `test_stops_after_max_tool_calls`,
  `test_stops_after_runtime_deadline`, timeout/unknown-tool → error message.
- **P08** — event ordering + sequence monotonicity.
- **P09** — conversation round-trip, `.md` completeness, no payload columns in SQLite,
  session deletion; **contract suite** run against in-memory and file/SQLite impls.
- **P10** — `test_resumes_from_checkpoint`,
  `test_does_not_duplicate_completed_tool`.
- **P12** — `test_stops_when_cancelled`, session-lock serialization,
  version-mismatch detection.
- **P13/P14** — workspace escape rejected; command timeout; env filtered; cwd preserved.
- **P15** — large output → artifact + short message.
- **P16** — context is system + recent window; compaction with a fake summarizer.
- **P16b/P16c** — skills + instructions acceptance lists in
  `docs/design/skills-and-instructions.md`.
- **P17** — `test_bootstrap_creates_full_object_graph`.
- **P20** — `test_runs_subagent`, `test_enforces_subagent_depth`,
  `test_parallel_subagents_run_concurrently`.
- **P26** — approval pause/resume/reject/replay.
- **P27** — invariant suite (§92), including **core never imports gateways** and
  **gateways never execute tools directly**.

---

## 5. Invariants as executable tests (P27 / land early where possible)

1. Every executed tool call has exactly one matching `ToolMessage`.
2. A session has at most one active run.
3. A completed run is immutable.
4. Messages are append-only.
5. Mutating tool executions have an idempotency identity.
6. Every run terminates via a known path.
7. Gateways never execute tools directly.
8. Core never imports Telegram / FastAPI / Textual.

---

## 6. Conventions

- Markers: `@pytest.mark.integration`, `@pytest.mark.llm` (already configured in
  `pyproject.toml`).
- Filesystem tests use `tmp_path`; **never** the real home/workspace.
- No test in `tests/` may require network unless marked `llm`.
- Smoke tests are manual, disposable, and never imported by `tests/`.
