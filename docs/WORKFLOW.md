# agent-stack — Development Workflow

> How work gets planned, executed, verified, and handed off in this repo. Modeled on the
> ticket-per-phase workflow used on earlier projects.

---

## 1. Units of work

agent-stack has no JIRA board. **The phase is the ticket.** Keys are `P<NN>` where `NN` is
the phase number in [`IMPLEMENTATION_PHASES.md`](IMPLEMENTATION_PHASES.md) — e.g. `P05`,
`P14`, `P20`. A milestone (`A`–`G`) is the epic.

```text
Epic   = milestone   (A..G)
Ticket = phase       (P00..P28+)
Branch = the phase key, e.g. `P05` (once git history starts)
```

If work is discovered that belongs in its own phase, draft a ticket file in `tmp/tickets/`
rather than scope-creeping the current phase.

---

## 2. The cycle

```text
1. Plan      → write tmp/plans/P<NN>-<title>.md BEFORE touching code
2. Implement → work the plan; adjust it when reality diverges
3. Spin off  → discovered work becomes tmp/tickets/<title>.md (a draft, not new scope)
4. Verify    → tmp/smoke_tests/P<NN>-<title>-smoke.py + make check  (BOTH green)
5. Handoff   → write tmp/handoff/P<NN>-<title>.md so a fresh chat can continue
6. Stop      → end the session; the next phase starts in a new chat from the handoff
```

The **smoke-test gate** applies to any phase that touches a running service, the DB, the
filesystem, or an external/LLM API — which for this project is almost every phase from
Milestone B onward.

### Multi-phase execution (subagent fan-out)

When several phases are executed by subagents, the same rules apply, plus:

- **Never launch a subagent with empty context.** Every subagent prompt must include:
  the phase's plan file path, the relevant section of `IMPLEMENTATION_PHASES.md`, the ADRs
  that constrain it, the exact files it owns, and the commands to verify.
- **Disjoint file ownership per wave.** Two subagents in one wave must not edit the same
  file. If they would, sequence them.
- **Respect dependencies.** A phase that depends on another does not start until the
  dependency's acceptance checks pass. See the dependency matrix in §5.
- Each subagent produces its own `tmp/handoff/P<NN>-*.md` and smoke result so the main
  session can review without re-reading the diff.

---

## 3. `tmp/` layout (local, gitignored)

`tmp/` is a **scratch/working area** — never committed. It must always be listed in
`.gitignore` (it is).

```text
tmp/
├── README.md          # this convention, short form
├── plans/             # P<NN>-<title>.md          — the plan written before code
├── tickets/           # <title>.md / P<NN>-<title>.md — drafted follow-up work
├── handoff/           # P<NN>-<title>.md          — cold-start context for the next chat
├── smoke_tests/       # P<NN>-<title>-smoke.py + P<NN>-<title>-smoke-result.md
└── docs/              # scratch docs: demo scripts, diagrams, TODO tracking
```

Naming is **phase-prefixed**: `P14-terminal-smoke.py`, never `terminal-smoke.py`.

---

## 4. Makefile targets

uv-native, skip-if-empty guarded >(a common pattern for optional tooling).

| Target | What it does |
|---|---|
| `make install` | `uv sync` |
| `make lint` | `ruff check` + `ruff format --check` on src and tests (no-op if no `.py` yet) |
| `make fmt` | import-sort + `ruff format` |
| `make typecheck` | `mypy src` (strict) |
| `make test` | `pytest -q tests` (exit 0 if nothing collected) |
| `make check` | lint + typecheck + test — **the commit gate** |
| `make run` | `python -m agent_stack` |
| `make clean` | remove caches/build artifacts (never touches `tmp/`) |
| `make plan PHASE=P09 TITLE=sqlite` | scaffold `tmp/plans/P09-sqlite.md` |
| `make handoff PHASE=P09 TITLE=sqlite` | scaffold `tmp/handoff/P09-sqlite.md` |
| `make smoke PHASE=P09 TITLE=sqlite` | scaffold `tmp/smoke_tests/P09-sqlite-smoke.py` |
| `make tree` | print the source tree |

`make plan/handoff/smoke` only create the empty scaffold with a header; the content is
written by hand.

---

## 5. Phase dependency matrix

A phase may start only when every phase it depends on has passed its acceptance checks.

| Phase | Depends on |
|---|---|
| P00 Foundation | — |
| P01 Model layer | P00 |
| P02 Tool registry | P00 |
| P03 Tool executor | P02 |
| P04 Agent definition | P00 |
| P05 AgentRuntime loop | P01, P03, P04 |
| P06 Errors & limits | P05 |
| P07 Sessions (metadata) | P04 |
| P08 Events | P05 |
| P09 SQLite index + file transcripts | P07 |
| P10 Checkpoints | P09 |
| P11 RunManager | P08, P10 |
| P12 Cancellation/locking | P11 |
| P13 Filesystem | P09 |
| P14 Terminal | P13 |
| P15 Artifacts | P13 |
| P16 Context builder | P09 |
| P16b Skills (SKILL.md) | P16 |
| P16c Instructions (AGENTS.md) | P16 |
| P17 Composition root | P11, P13, P15, P16 |
| P18 Application layer | P17 |
| P19 Gateway contract | P18 |
| P20 Subagents | P17, P19 |
| P21 Memory | P17 |
| P22 Planning/TODOs | P17 |
| P23 CLI/TUI | P19 |
| P24 Web | P19 |
| P25 Telegram | P19 |
| P26 Approval | P20, P25 |
| P27 Observability/invariants | all |
| P28 Hardening | all |
| P33 Queued messages, TUI tabs | P20, P23 |
| P34 TUI refactor | P33 |
| P35 Tool timeouts | P03, P14 |
| P36 Structured auto-approval | P26 |
| P37 Prompt caching | P01, P31 |
| P38 `web_screenshot` | P15 (P44 for image results) |
| P39 Web multi-turn rewind | P24, P30 |
| P40 Gateway rate limits | P24, P25 |
| P41 `/export` | P34 (TUI part) |
| P42 OpenTelemetry | P08, P27 |
| P43 MCP client | P02, P26 |
| P44 Images in | P34, P38 |
| P45 Token streaming | P08, P24, P29 |
| P46 Subagent conversations in their chat | P09, P20 |
| P47 Web subagent panel | P24, P45, P46 |
| P48 No step cap, subagent reports, TUI subagent screen | P06, P20, P46 |
| P49 Tool permissions for skills and subagents | P16b, P20, P26 |
| P50 Storage lifecycle (delete and gc) | P09, P15, P29, P30, P46 |

---

## 6. Handoff pattern (condensed)

A handoff exists for one reader: a fresh chat about to start the **next** phase. Optimize
for "pick up cleanly", not a transcript.

Required sections:
- **Title** + date.
- **What was completed** — concrete result + state (implemented / smoke-tested / committed).
- **Key findings** — bugs, gotchas, non-obvious decisions (often the most valuable part).
- **Next phase** — key, one-line summary, why it's next; say "start a new chat with
  dev-workflow against P<NN>".
- **Must not get wrong** — sharp edges.
- **Repo state right now**.
- **Templates/patterns to mirror** — file paths to reuse.
- **Things NOT to do** — explicit anti-scope.
- **References** — phase keys, docs, ADRs.

## 7. Smoke-test pattern (condensed)

A smoke test exercises the **real** thing with nothing mocked on the changed path: a real
DB, a real running process, real filesystem, and — for this project — **the real LLM**
(token cost is not a constraint; model-behavior bugs are the point). It is manual and
disposable, lives in `tmp/smoke_tests/`, and never runs in CI.

Report order: title+date → context (what was real vs. stubbed, and why) → script reference
→ `| Check | Result |` table → explicit **Overall: ALL PASS / FAILED:** → bugs found →
"also ran `make check`".

**Commit gate:** do not commit or push until BOTH the smoke test and `make check` are green
(and only when explicitly asked to commit).

---

## 8. Commits

Once git history begins:

```text
<phase-key> <imperative summary>      e.g.  P05 Implement the agent loop
```

No AI/assistant references or `Co-Authored-By` trailers. One phase per commit (Appendix E
of `IMPLEMENTATION_PHASES.md`).
