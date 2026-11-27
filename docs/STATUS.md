# Status snapshot (2026-11-27)

Where the project stands and what to do next. Written so a fresh session can pick it up
cold. `git log` has the later truth.

## State

- 856 tests, `make check` (ruff + strict mypy + pytest) green; web `npm test` (38) and
  `npm run build` green. Optional extras: `--extra browser` (`web_screenshot`), `--extra otel`
  (tracing); without them those tests skip. Private repo, `main`. `.claude/skills/` is local only (gitignored, removed from history).
- Run it: `agent-stack` (TUI), `agent-stack chat` (line REPL), `agent-stack telegram`
  (bot), `agent-stack audit tail`. Default model `openrouter:deepseek/deepseek-v4-pro`
  (`AGENT_STACK_MODEL`); see `.env.example` for every variable.

| Area | Phases | Notes |
|---|---|---|
| Runtime core | 0-20 | loop, limits, deadline, tool timeout, sessions, events, checkpoints, run manager, subagents, skills, instructions, memory files |
| Memory and tasks | 21, 22 | `remember`/`recall`/`forget` (SQLite, per user); `todo_*` tools (per session) |
| Gateways | 23, 25 | TUI (themes, cards, conversations picker, approvals), Telegram (allow-listed; pages and opens any conversation, tool-call viewer, /verbose modes, rename/delete, HTML replies, restart-safe state) |
| Trust | 26, 27, 28, 42 | approval flow; redaction, JSON logs, metrics, eight invariants; hardening matrix; optional OpenTelemetry traces (`--extra otel`) |
| Event log, branching | 29, 30 | durable redacted `event_log` table + `Application.replay`; `Application.branch_session` forks a conversation at a logged event |
| Large output | 15 | results over 20 KB become artifacts; `read_artifact` pages through them |
| Web | 24 | FastAPI + resumable SSE, Next.js UI (`web/`) |
| Context engineering | 16, 16e, 31 | resident parts, calibrated token budget, tool-result clearing, saved compaction + `/compact` |
| Chat search | 32 | `find_chats`: FTS5 + BM25 over every conversation's full text |
| Live interaction | 33, 45 | messages queued into a working run (take back to edit/cancel); tokens stream to the web UI and TUI (`assistant.delta`, live-only, never logged) |
| TUI | 33, 34 | tabs (each runs on its own), sidebar with one context meter, model, mode; split into modules by concern |
| Tools | 35, 38, 43, 52 | per-call timeout (`AGENT_STACK_TOOL_TIMEOUT`); `web_screenshot` (Playwright); MCP servers from `.mcp.json` as `mcp__<server>__<tool>`, restarted after a lost connection, tool list refreshed on `list_changed`, closed by every gateway on exit |
| Models | 36, 37, 44, 51 | auto-mode verdicts from Jev's decisions endpoint (a chat model via `with_structured_output` still works); prompt-cache breakpoints (Claude, Qwen; others cache by themselves); images in (by reference under `artifacts/`, ADR 019); transient errors (429, 5xx, timeouts) retried by the runtime with backoff and Retry-After, then `AGENT_STACK_FALLBACK_MODEL` (`model.retry` event shown in every gateway) |
| Gateways (more) | 39, 40, 41 | web rewind from any message; per-principal rate limits (429 / slow down); `/export md\|json` everywhere |

## Since the phases (unphased work, all in `git log`)

- **History:** rewind (code, conversation or both), fork, switch files, branch tree, `asref://`
  references, tools to find/read/ask/fork old chats; in every gateway.
- **Auto mode** (default): rules, then the Jev classifier, then you. The classifier sees a
  redacted request and call only. Audit fixes on 2026-11-25 closed rule bypasses, kept monitor
  notes out of its view, and redacted Telegram tool arguments and errors.
- **`/model`** switches the main model for the next runs (TUI, Telegram, web, until restart).
- **Monitors, `web_search`, `web_fetch`** (reader mode by default), ideas inbox, headless runs.
- **Built-in skills** `handoff` and `smoke-test` ship in `src/agent_stack/skills/builtin/`.

## Context engineering (where it stands)

What the model sees each call: the base prompt, then AGENTS.md/CLAUDE.md, skill metadata and the
MEMORY.md index (capped at 64 KB), then the latest compaction summary, then the messages after
it, with the run's request pinned first. The size estimate counts tool schemas and is calibrated
against the provider's reported input tokens. Past 80% of `AGENT_STACK_CONTEXT_BUDGET` (120k) the
conversation's own model summarises it from its real input and the summary is saved in the
transcript with the files in use (P31; `AGENT_STACK_COMPACT_AT`, `off` disables); past 100% old
tool results are cleared and then the oldest messages dropped with a note. Old chats are found by
ranked full-text search (P32). Not built: semantic (embedding) retrieval over old chats,
automatic memory extraction. Not yet run against a real model: compaction (tests only).
## Small model for side work

- `AGENT_STACK_SMALL_MODEL` (ADR 014): the `explorer` subagent and conversation titles use a
  cheaper model; answers and all file edits use the main model. Cost totals show the per-model
  split (`/cost` on Telegram, the web chip's tooltip). `main` turns it off.
- Fixed on the way: renaming a conversation while a run was going was silently undone when the
  run saved its session at the end.

## Files the agent changed (TUI, web)

- `write_file`/`edit_file` report each change (text before and after) through the run context to a
  change store: SQLite indexes ids and paths, the snapshots live in `.agent/changes/`
  (ADR-008). `Application.changed_files` / `changed_file` give the net diff per file (a run or the
  whole conversation, subagent runs included).
- TUI: a write/edit card opens on the diff of that call; `ctrl+g` lists every changed file with
  its net diff. Web: a "Files" panel (Diff / File / Rendered tabs, markdown and mermaid rendered),
  "View changes" on the cards, and a diff preview on the approval card before anything runs.
- Limit, stated in both UIs: only changes made through the two file tools are tracked, not files
  changed by shell commands (`run_command`). Binary or oversized files are not recorded.
- The web UI renders markdown with GFM, highlighted code and mermaid (loaded on first use,
  `securityLevel: strict`); remote images are not loaded (a prompt-injected URL could carry
  data out) and links open in a new tab without a referrer.

## Cost (every gateway)

- A `model.usage` event follows each model call (tokens, and the provider's own cost when it
  reports one, as OpenRouter does). `Application.session_usage` sums the durable event log, so a
  conversation's total survives restarts and includes subagent runs. TUI (hints + sidebar), line
  REPL (`/cost`), Telegram (`/cost`, per-run cost on the finished status) and the web API/UI
  (`/sessions/{id}/usage`, header chip) show it.
- `AGENT_STACK_BUDGET_USD` caps a conversation: at or over it, no further run starts (HTTP 402 on
  the web API). It is checked when a run starts, not mid-run.
- Honest limits: an unpriced call (a local model) is counted but never guessed at; a call that is
  cancelled mid-flight records no usage, so any partial charge for it is not counted.

## Not done

- **Follow-ups from P34-P45** are drafted in `tmp/tickets/followups-P34-P39.md` (local): web cost
  line without cache numbers, delta redaction across chunk boundaries, MCP resources/prompts
  (`tmp/tickets/P52-followups.md`),
  tracing of parallel tools, image downscaling and original names, and more.

- **P24 Web gateway: done** (kept here for its notes). `agent-stack web` serves the API
  (`gateways/web/`): sessions, messages, a resumable SSE stream (`Last-Event-ID`), cancel (also
  stops subagents), approvals, branching. It needs `AGENT_STACK_WEB_TOKEN`, binds to loopback,
  and treats other principals' sessions as nonexistent. The Next.js app (`web/`, style per ADR 013)
  has live tool cards, approvals, subagent nesting, a resumable stream, fork and a stop button;
  `cd web && npm run dev` (needs `AGENT_STACK_WEB_TOKEN` entered on the connect screen). Checked in
  a real browser at 360/768/1280/1600 px, light and dark, against the real model. No visual
  regression suite yet.
- **P29/P30 gaps (the phases themselves are done).** Not built, by choice: payload
  `offset` pointers into `transcript.jsonl` and a `branch_id` column (a branch is its own
  session, so `session_id` is the key; the log keeps redacted, capped event data instead of
  offsets); branch merge/diff. (Dead-branch GC: every branch is a listed conversation, so none
  is unreachable; P50's `agent-stack gc` removes the data no session reaches.) (A TUI/Web "fork here" affordance was listed
  here as not built; it was built later with the history work: rewind pickers, branches views,
  the web History panel and "Fork a branch from here".)
  Branch granularity: messages are saved when a run ends, so forking at an event inside a run
  gives the conversation as it stood *before* that run; fork at `run.completed` to keep it.
  Branches do not copy `artifacts/` (workspace-scoped) and show up in the picker as
  "<title> (branch)". The tests that build the app need `OPENROUTER_API_KEY` set (any value).
- **Public release** (the repo is private on purpose): README with an architecture diagram and
  a demo recording, LICENSE, an ADR "why not just LangGraph", and a fresh scan of the whole
  history for employer or customer names before the visibility changes.

## Verification status

Verified for real on 2026-11-14 (cheap models via OpenRouter, small spend):

- **Real-model runs:** the line REPL, the web API and the web UI each ran real tasks (a file
  read, an approved shell command, a subagent, a stop mid-generation, cost accounting, a markdown file with a mermaid diagram written and edited
  with approvals, then read back through the files panel).
- **Telegram against the real Bot API:** the bot registered its command menu and the owner
  confirmed `/start`, replies, the tool-call viewer and per-run cost in a real chat.

Verified for real by the owner on 2026-11-14, 2026-11-25 and 2026-11-27 (the stored TUI
conversations and the audit log show it):

- **The TUI against real models in a real terminal** (GLM, Kimi, DeepSeek via OpenRouter): the
  approval dialog approved and rejected gated calls by hand, auto mode decided through Jev,
  `explorer` subagents ran, a subagent's context was auto-compacted, and the look and feel was
  reviewed.

Still unverified (do these next):

- **The Telegram inline Approve/Reject buttons** in a real chat.

## Known limits (written down, not hidden)

- Artifacts are workspace-scoped: deleting a conversation leaves its large-output files until
  `agent-stack gc --apply` (P50) finds nothing references them; GC never runs by itself, and
  keeps anything touched in the last 24 hours. Folders the index has no row for are reported,
  never deleted (rules: `docs/design/conversation-storage.md` section 9).
- If recording a tool batch fails *after* the tools ran, a retry with the same run id could run a
  mutating tool again. History stays valid because the failed run saves no tool results.
- A subagent gets only the tools its parent has (and its parent's active skill allows), and every
  call its parent would have to approve needs approval in the subagent too (P49); that is what
  makes the write-capable `coder` subagent safe. A skill's `allowed-tools` only restricts, never
  pre-approves; it lasts for the run that loaded the skill, and Claude Code tool names (`Read`,
  `Bash(git:*)`) do not match ours, so such a skill can call nothing but the skill tools.
- Tool output shown to the model is scrubbed with `redact(pairs=False)`: known env secrets and
  common key shapes only, so a secret in an unusual shape passes.
- The `message_index` and `artifacts_index` tables are in the schema and `SqliteIndex` is
  tested, but the app never writes them (no query has needed them).
- `chat_fts` (P32) keeps a copy of message text in `agent.db`; deleting a conversation removes
  its rows. The transcripts stay the source of truth.
- `.claude/CLAUDE.md` is not read as instructions (`AGENTS.md`, `CLAUDE.md` and the `.local.md`
  overlays are). Two local skills are skipped because their frontmatter name differs from their
  folder (`image-to-code-skill`, `taste-skill`).
- Compaction (P31) has only been tested with the scripted model, not a real one.

## Decisions already made (do not relitigate)

- The two commits titled "P27" stay as they are (the owner chose not to squash).
- Telegram requires an explicit user allow-list and refuses to start without one. It serves an
  allow-listed user only in their own private chat (never a group or channel, where members
  would see the output); strangers get no reply and are logged locally (id and reason, never
  content) at most once a minute.
- Default tools that need approval: `run_command`, `write_file`, `edit_file`, `web_fetch`,
  `monitor_start` (`AGENT_STACK_APPROVAL_TOOLS`; empty runs everything unattended). The default
  approval mode is **auto** (2026-11-25): rules, then the classifier, then you. This supersedes
  the earlier "always ask" default.
- Every commit is authored as `aayushjoshi-12` with no `Co-Authored-By`/AI trailer; history was
  rewritten to match on 2026-11-25.
- `.claude/skills/` stays on the owner's machine: gitignored and removed from all history on
  2026-11-26. Skills agent-stack ships live in `src/agent_stack/skills/builtin/`.
- Compaction uses the conversation's own model, never the small one (ADR 015, supersedes the
  first P31 cut that used the small model). Chat search is FTS5 + BM25 over a disposable index
  (ADR 016).
- A launch opens a fresh draft conversation; old ones come from `ctrl+o`.
- Items from the ponytail audit deliberately skipped: file/in-memory `list_for_owner` (kept for
  the store contract), the `AGENT_STACK_CONTEXT_WINDOW` knob, `memory/store.py` typing nits,
  the early raise in `_within`.

## Working notes that cost time once

- `make check | tail ... && git commit` hides a failing step (the pipe reports `tail`). Check
  the real exit status: `make check >/tmp/g.txt 2>&1; echo $?`.
- A new test that passes first try proves nothing: revert the fix and watch it fail. Several
  fixes here were verified that way. When mutating code, make sure the edit actually applied
  (the formatter reshapes code and silently defeats string replaces).
- A fake whose `await` never yields starves the event loop and hangs the suite; `await
  asyncio.sleep(0)` in poll-style fakes.
- A crash is a `BaseException` in tests; an ordinary exception is a *failed run*, which keeps
  its history.
- macOS `sed -i` needs `-i ''`. `gh` must be logged in as the personal account for this repo.
- `tmp/` is gitignored scratch (plans, tickets, handoffs, the screenshot harness
  `tmp/tui_shots.py`, the pty review `tmp/tui_pty_review.py`).
