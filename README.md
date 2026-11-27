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

- `web_search` (DuckDuckGo, no key) and `web_fetch`. By default a free reader service (r.jina.ai)
  fetches the page, so this machine never connects to arbitrary hosts, but that service sees the
  URL; `AGENT_STACK_FETCH_MODE=direct` fetches locally instead (private addresses refused). Each
  fetch needs approval and results are labelled untrusted.
- `web_screenshot` opens a page in headless Chromium and saves a PNG artifact (path, title,
  status and console errors come back as text). `localhost` and LAN hosts are allowed, cloud
  metadata and link-local are not; each call needs approval. Optional:
  `uv sync --extra browser && uv run playwright install chromium`; without it the tool is left out.
- `monitor_start` watches a command in the background with no model calls and leaves a note only
  when the output changes, matches a pattern, or the command succeeds; notes appear at the top of
  your next message. Stops itself after an hour or ten notes.
- History, in every gateway. **TUI:** press `esc esc` (on an empty prompt) to pick an earlier
  message and restore code, conversation or both; the message comes back in the input box. `ctrl+p`
  has branches, copy a reference, and put this conversation's files in the folder; pasting an
  `asref://` line opens it. **Telegram:** `/rewind`, `/branches`, `/ref`, `/files`. **Web:** `esc esc`
  or the History button (rewind, a drawn branch graph, share a point). **CLI:**
  `agent-stack turns|tree|fork|rewind|switch|ref ...`. A rewind forks at the point and
  puts the files back; the original conversation keeps everything after it, and `switch` returns
  the folder to any branch. `agent-stack tree <session> --html tree.html` draws the branches.
  `agent-stack ref make <session> <turn>` prints an `asref://` string another chat can read with
  `read_reference` or open with `ref open`. Only `write_file`/`edit_file` changes are tracked,
  not files changed by shell commands.

## Slash commands and auto mode

- Type `/` in the TUI prompt, the web composer or Telegram for a list: built-ins (`/new`,
  `/rewind`, `/branches`, `/ref`, `/files`, `/mode`, `/model`, `/compact`) and every skill. `/skill-name your request`
  runs the skill with its instructions attached; the chat shows only what you typed. Tab or enter
  completes. `/model provider:name` switches the model for the next messages (until a restart;
  `AGENT_STACK_MODEL` stays the default); `/model` alone shows the current one.
- **Compaction.** Past 80% of the context budget (about 96k of the default 120k tokens;
  `AGENT_STACK_COMPACT_AT`, `off` to disable) the conversation's own model summarises everything
  so far from exactly the input it was about to see, and the summary is saved in its place, as
  in Claude Code and opencode. The model then sees the summary, the files it was working on (as
  they are now) and what follows; the transcript keeps every message for the UIs, rewind and
  forks, where the compaction shows as its own row. `/compact [focus]` does it on demand (TUI,
  line REPL, Telegram, web).
- **Built-in skills.** `/handoff` writes `tmp/handoff/<date>-<title>.md` so a fresh chat can carry
  on cold; `/smoke-test` runs a real smoke test with the `.env` credentials and writes a report in
  `tmp/smoke_tests/`. They ship in `src/agent_stack/skills/builtin/`; a skill of yours with the
  same name wins.
- **Auto mode** (`shift+tab` in the TUI prompt, `/mode auto`, the header button on the web). A tool
  call that would ask you is first judged by rules (catastrophic: refused; sudo, pushes,
  credentials, installs: always asked; plain reads and ordinary edits inside the workspace:
  allowed) and then by Jev, OpenRouter's decision model, which picks allow, deny or ask
  (`AGENT_STACK_AUTO_MODEL`, default `decisions:~typesafe/jev-latest`; under 70% sure it asks). The
  classifier sees only your request and the call, never tool output or web pages, and anything it
  is unsure about, or any failure, comes back to you with its reason. Every decision is shown.
  It is a guard against accidents and prompt injection, not a sandbox.
- **Outside the workspace:** by default (`AGENT_STACK_EXTERNAL_ACCESS=ask`) any tool may reach
  outside the folder, but each such call needs your approval or an auto-mode verdict.
- `agent-stack resume [id]` continues a conversation; the TUI prints the command when you quit.
  The agent can also look back itself: `find_chats` (full-text search of every chat, ranked with
  BM25 over a SQLite FTS5 index), `read_chat` (tail or summary), `ask_chat`
  (a question answered from an old chat in a separate call) and `fork_chat`.

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
