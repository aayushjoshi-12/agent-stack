# Status snapshot (paused 2026-11-13)

Where the project stands and what to do next. Written so a fresh session can pick it up
cold. Everything below was true at commit `2be1c43`; `git log` has the later truth.

## State

- 517 tests, `make check` (ruff + strict mypy + pytest) green. Private repo, `master`, in sync.
- Run it: `agent-stack` (TUI), `agent-stack chat` (line REPL), `agent-stack telegram`
  (bot), `agent-stack audit tail`. Default model `openrouter:deepseek/deepseek-v4-pro`
  (`AGENT_STACK_MODEL`); see `.env.example` for every variable.

| Area | Phases | Notes |
|---|---|---|
| Runtime core | 0-20 | loop, limits, deadline, tool timeout, sessions, events, checkpoints, run manager, subagents, skills, instructions, memory files |
| Memory and tasks | 21, 22 | `remember`/`recall`/`forget` (SQLite, per user); `todo_*` tools (per session) |
| Gateways | 23, 25 | TUI (themes, cards, conversations picker, approvals), Telegram (allow-listed; pages and opens any conversation, tool-call viewer, /verbose modes, rename/delete, HTML replies, restart-safe state) |
| Trust | 26, 27, 28 | approval flow; redaction, JSON logs, metrics, eight invariants; hardening matrix |
| Event log, branching | 29, 30 | durable redacted `event_log` table + `Application.replay`; `Application.branch_session` forks a conversation at a logged event |
| Large output | 15 | results over 20 KB become artifacts; `read_artifact` pages through them |

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

- **P24 Web gateway: backend and UI built (`web/`, Next.js).** `agent-stack web` serves the API
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
  offsets); a TUI/Web "fork here" affordance
  (gateways call `Application.branch_session`); branch merge/diff and dead-branch GC.
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
  read, an approved shell command, a subagent, a stop mid-generation, cost accounting).
- **Telegram against the real Bot API:** the bot registered its command menu and the owner
  confirmed `/start`, replies, the tool-call viewer and per-run cost in a real chat.

Still unverified (do these next):

- **The TUI against a real model in a real terminal**, including reopening a conversation with
  `ctrl+o`. It is tested headlessly and in the screenshot harness only.
- **The approval dialog in a real terminal**, and the Telegram inline Approve/Reject buttons in a
  real chat (the web UI's approval was exercised for real).

## Known limits (written down, not hidden)

- Artifacts are workspace-scoped: deleting a conversation leaves its large-output files until
  the garbage collection the plan defers.
- If recording a tool batch fails *after* the tools ran, a retry with the same run id could run a
  mutating tool again. History stays valid because the failed run saves no tool results.
- A subagent does not inherit its parent's tool policy. Fine while `explorer` is read-only; a
  write-capable subagent would bypass approval.
- The approval broker never prunes finished requests (memory only; fine for one owner).
- Tool output shown to the model is scrubbed with `redact(pairs=False)`: known env secrets and
  common key shapes only, so a secret in an unusual shape passes.

## Decisions already made (do not relitigate)

- The two commits titled "P27" stay as they are (the owner chose not to squash).
- Telegram requires an explicit user allow-list and refuses to start without one. It serves an
  allow-listed user only in their own private chat (never a group or channel, where members
  would see the output); strangers get no reply and are logged locally (id and reason, never
  content) at most once a minute.
- Default tools that need approval: `run_command`, `write_file`, `edit_file`
  (`AGENT_STACK_APPROVAL_TOOLS`; empty runs everything unattended).
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
