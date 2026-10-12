# agent-stack

A personal, extensible agent runtime built from first principles on top of LangChain
primitives (`init_chat_model`, `BaseChatModel`, `BaseTool`, LangChain messages).

The goal is a system small enough to understand completely, structured enough that
features (durability, subagents, memory, gateways) can be added without rewriting the
core.

## Docs

- **[docs/IMPLEMENTATION_PHASES.md](docs/IMPLEMENTATION_PHASES.md)** — the ordered build
  plan (28 phases, 7 milestones) with acceptance criteria. **Start here.**
- [docs/design/source-design-document.md](docs/design/source-design-document.md) — the
  original architecture design document.
- [docs/decisions/](docs/decisions/) — architecture decision records.

## Core ideas

- `Agent` = immutable configuration · `Session` = durable context · `Run` = one execution
  · `AgentRuntime` = executes an `Agent` · `Gateway` = talks to users · `bootstrap.py` =
  wires it all together.
- One composition root (`app/bootstrap.py`); plain constructor injection, no DI framework.
- One explicit async model/tool loop that persistence, events, limits, and subagents
  surround rather than contaminate.

## Development

```bash
make install   # uv sync
make check     # lint + typecheck + tests
make fmt       # format
```

## Run

```bash
make install-tool   # one-time: put `agent-stack` on PATH (editable)
agent-stack         # launch the TUI (single command)
```

`agent-stack` opens the **current directory as its workspace** — the agent's file,
shell, memory and session tools are scoped to where you run it. Subcommands:

```bash
agent-stack tui              # the Textual TUI (same as no args)
agent-stack chat             # line REPL (scriptable; reads stdin)
agent-stack audit tail       # follow the JSONL audit log
```

Without installing, use `uv run agent-stack` (or `uv run python -m agent_stack`).
Point it elsewhere with `AGENT_STACK_WORKSPACE=/path` (`AGENT_STACK_DB` overrides the
SQLite index path; `AGENT_STACK_MODEL` the model).

The TUI inherits your terminal's colours (no palette of its own). It assumes a dark
terminal; on a light terminal set `AGENT_STACK_THEME=ansi-light` (see
[ADR 011](docs/decisions/011-terminal-native-theme.md)).

## Layout

```text
src/agent_stack/
├── core/         # Agent, AgentRuntime, Session, Run, events, state
├── models/       # model factory + capabilities
├── tools/        # registry, executor, builtins
├── persistence/  # store protocols + implementations
├── memory/       # cross-session memory
├── artifacts/    # large output storage
├── subagents/    # delegation
├── gateways/     # CLI/TUI, Web, Telegram
└── app/          # config, application service, bootstrap (composition root)
```
