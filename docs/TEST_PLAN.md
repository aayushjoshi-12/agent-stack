# agent-stack — Test Plan

> Coverage strategy. The runtime must be testable **without real LLM calls**; real-model tests
> are opt-in. Sections 3 and 4 were the plan written before the build; §3 now records what exists
> (2026-11-26), §4 is kept as the record of what each phase had to prove.

---

## 1. Layers

| Layer | What | Location | Runs in CI? |
|---|---|---|---|
| **Unit** | registry, executor, state, limits, serializers, session resolution | `tests/unit/` | yes |
| **Contract** | one suite run against every implementation of a store protocol (in-memory + SQLite) | `tests/contract/` | yes |
| **Invariant** | the runtime guarantees in §92 | `tests/invariants/` | yes |
| **Integration** | real SQLite, real filesystem, real subprocess (fake model) | `tests/integration/` | yes |
| **LLM** | real provider call | `tests/integration/` (`@pytest.mark.llm`) | opt-in (none written yet: real-model checks live in `tmp/smoke_tests/`) |
| **Gateway** | TUI (Textual pilot), Telegram (fake Bot API), web (ASGI client), line REPL | `tests/unit/`, `tests/integration/` | yes (fake model) |
| **Web UI** | the event reducer and helpers (`node --test`) | `web/lib/*.test.ts` | `cd web && npm test` |
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

## 3. Coverage — what exists (2026-11-26)

749 tests: 47 unit files, 17 integration, 1 contract suite (in-memory and SQLite stores), 1
invariant suite; plus 4 web test files. The table that stood here listed every core module as
"none" (true on 2026-10-11, before the build) and is superseded. `make check` runs ruff (rules
and formatting), strict mypy and the whole suite. Gaps that remain:

- No `@pytest.mark.llm` test: real-model behaviour is checked by hand in `tmp/smoke_tests/`.
- No end-to-end directory (`tests/e2e/` was planned): gateway tests drive the real
  `Application` with the fake model instead.
- No visual regression suite for the web UI or the TUI (screenshots are checked by hand).

---

## 4. Coverage per phase (the plan; all phases are built)

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
  *Superseded by P31:* compaction is tested with the scripted model summarising its own input
  (`tests/unit/test_context_wiring.py`); P32 search in `tests/integration/test_chat_search.py`.
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
