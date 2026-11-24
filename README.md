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

## Ideas inbox

One place for ideas from every project: `agent-stack ideas add <project> <title> <text>`,
`agent-stack ideas list [--project P]`, `agent-stack ideas show <project> <title>`. The agent can
use the same inbox through its `add_idea`, `list_ideas` and `read_idea` tools. Files live in
`~/.agent-stack/ideas` (`AGENT_STACK_HOME`).

## Headless runs

`agent-stack run [--yes] <prompt|->` does one task without a UI. `--yes` approves every gated
tool call, so only use it in a container. See `docs/BENCHMARKS.md` for how it was measured.

## Web, monitors, history

- `web_search` (DuckDuckGo, no key) and `web_fetch` (public http/https only; private addresses
  are refused, and each fetch needs approval). Results are labelled untrusted.
- `monitor_start` watches a command in the background with no model calls and leaves a note only
  when the output changes, matches a pattern, or the command succeeds; notes appear at the top of
  your next message. Stops itself after an hour or ten notes.
- History: `agent-stack turns|tree|fork|rewind|switch|ref ...`. A rewind forks at the point and
  puts the files back; the original conversation keeps everything after it, and `switch` returns
  the folder to any branch. `agent-stack tree <session> --html tree.html` draws the branches.
  `agent-stack ref make <session> <turn>` prints an `asref://` string another chat can read with
  `read_reference` or open with `ref open`. Only `write_file`/`edit_file` changes are tracked,
  not files changed by shell commands.

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
