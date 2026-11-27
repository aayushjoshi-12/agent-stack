# AGENTS.md

Guidance for coding agents working in this repo. This is also the format agent-stack itself
learns to load (see `docs/design/skills-and-instructions.md`) — keep it accurate.

## What this is

`agent-stack` (`src/agent_stack/`) is a personal, extensible agent runtime built from first
principles on LangChain primitives (`init_chat_model`, `BaseChatModel`, `BaseTool`,
LangChain messages). One runtime, many gateways (CLI/TUI, Web, Telegram).

**Read first:** `docs/IMPLEMENTATION_PHASES.md` (the build plan) and
`docs/WORKFLOW.md` (how work is done). Architecture decisions live in `docs/decisions/`.

## Non-negotiable rules

- **One composition root.** Only `app/bootstrap.py` constructs the object graph. No class
  builds its own dependencies.
- **One-way dependencies.** `gateway → application → run manager → runtime → model/tools/stores`.
  Core code never imports FastAPI, Telegram, or Textual.
- **Three kinds of things:** definitions (frozen config), runtime objects, persistent data.
  Never put mutable execution state on `Agent`.
- **Abstract only at infrastructure boundaries** (`SessionStore`, `RunStore`,
  `CheckpointStore`, `MemoryStore`, `ArtifactStore`, `EventBus`, `ToolRegistry`) and only
  when forced.

## Commands

```bash
make install    # uv sync
make check      # lint (rules + formatting) + typecheck + test  (the commit gate)
make fmt        # import-sort + ruff format
make run        # python -m agent_stack
```

## Conventions

- Python 3.12+, `uv`, async-first. `ruff` + strict `mypy`.
- Type everything; prefer `dataclass(frozen=True)` for definitions.
- Keep the model/tool loop explicit and boring; persistence/events/limits surround it.
- Sessions are **files** (`transcript.jsonl`/`.md` + `artifacts/`); SQLite is only the index.
- **Write code in ponytail mode** (`.claude/skills/ponytail/SKILL.md`): the smallest change
  that fully solves the task; no speculative abstraction, no dead config, no re-export
  layers nothing imports. Design notes: `docs/design/` (`file-capabilities.md`,
  `agent-and-tool-prompts.md`, `skills-and-instructions.md`,
  `event-log-and-branching.md`, `conversation-storage.md`).

## Testing

- Runtime is testable **without** real LLM calls: use the deterministic fake model in
  `tests/`.
- Mark real-LLM tests `@pytest.mark.llm`; they are opt-in.
- Smoke tests (real deps, no mocks on the changed path) live in `tmp/smoke_tests/`.

## How work is done

**Phase or fix? Decide first.**
- A **phase** (`P<NN>`, the next free number in `docs/IMPLEMENTATION_PHASES.md`) is only for big
  work or a new feature. Examples: a new capability, a new storage layout, a multi-part build
  across layers.
- A **fix** is everything else: polish, UI tweaks, key behaviour, bug fixes, small follow-ups.
  It gets no phase number and no phase docs. Fix it, gate it, commit it with a plain descriptive
  message.
- When unsure, it is a fix.

**A phase** follows `docs/WORKFLOW.md`:
1. Plan in `tmp/plans/P<NN>-<title>.md` before code.
2. Implement in ponytail mode.
3. Draft discovered work in `tmp/tickets/`; do not grow the phase.
4. Smoke with real dependencies (a real model, the real web UI through Playwright, the real
   app) in `tmp/smoke_tests/P<NN>-*.py`.
5. Add the phase to `docs/IMPLEMENTATION_PHASES.md` (Goal, Deliverables, Acceptance, Built), a
   dependency row to `docs/WORKFLOW.md`, and an ADR when a decision is structural.
6. Commit as `P<NN> <what it does>`.
7. Write `tmp/handoff/P<NN>-<title>.md` when the next step belongs to a new chat.

A request listing several features becomes several phases (or fixes), one commit each.

**The gate.** `make check` must pass before every commit: check its exit code, never the exit
code of a `| tail`. When `web/` changed, also run
`cd web && npm test && npx tsc --noEmit && npm run build`.

**Verify the way the user sees it.**
- UI changes: render them (`tmp/smoke_tests/tui-look.py` for the TUI, Playwright screenshots for
  the web) and look at the image before calling them done.
- Real-model behaviour (limits, timeouts, approvals): needs a real run. A green unit test does
  not prove it.
- Report what was verified, and what was not, plainly.

**Subagents.** Only when the user asks, or when phases are independent enough to run in
parallel.
- Give each the plan path, its section of the phase plan, the files it owns and the verify
  commands.
- Agent worktrees start from `origin/main`: have them `git reset --hard main` first.
- Merge by rebasing onto local `main` and `git merge --ff-only`, then run the gate on `main`.

**Writing.** No emojis in UI strings, bot replies, commit messages or docs.

## Action guards

- Commit only when asked. During a build the user asks for a commit per phase or fix, on local
  `main`. Never push unless asked.
- The author is the repo owner only. Never add AI or `Co-Authored-By` trailers.
- An unpushed commit of your own may be amended to fix it; never rewrite pushed history.
- `tmp/` is gitignored scratch (plans, handoffs, smoke tests, tickets). Never commit it, and
  never commit `.env` or `node_modules` links.

## Directory map

```
src/agent_stack/
├── core/         # Agent, AgentRuntime, Session, Run, events, context, locks
├── models/       # model factory
├── tools/        # registry, executor, builtins
├── persistence/  # store protocols + sqlite index + file conversation store
├── memory/       # cross-session memory
├── artifacts/    # large-output storage
├── skills/       # SKILL.md discovery + progressive loading
├── context/      # AGENTS.md loading + context building
├── subagents/    # delegation
├── gateways/     # CLI/TUI, Web, Telegram
└── app/          # config, application service, bootstrap (composition root)
```

Note: `.claude/skills/` holds repo-scoped skills (the web design skills plus the ponytail
working-mode skill). It is gitignored: the skills live on the owner's machine only and are not
in the repository or its history; never reference the external source repos.
