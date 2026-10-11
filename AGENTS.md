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
make check      # lint + typecheck + test  (the commit gate)
make fmt        # import-sort + ruff format
make run        # python -m agent_stack
```

## Conventions

- Python 3.12+, `uv`, async-first. `ruff` + strict `mypy`.
- Type everything; prefer `dataclass(frozen=True)` for definitions.
- Keep the model/tool loop explicit and boring; persistence/events/limits surround it.
- Sessions are **files** (`transcript.jsonl`/`.md` + `artifacts/`); SQLite is only the index.

## Testing

- Runtime is testable **without** real LLM calls: use the deterministic fake model in
  `tests/`.
- Mark real-LLM tests `@pytest.mark.llm`; they are opt-in.
- Smoke tests (real deps, no mocks on the changed path) live in `tmp/smoke_tests/`.

## Action guards

- Don't commit or push unless asked (the user does ask during this build — then commit per
  phase). Never add AI/`Co-Authored-By` trailers.
- `tmp/` is gitignored scratch (plans, handoffs, smoke tests). Never commit it.

## Directory map

```
src/agent_stack/
├── core/         # Agent, AgentRuntime, Session, Run, events, state, context
├── models/       # model factory + capability validation
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
