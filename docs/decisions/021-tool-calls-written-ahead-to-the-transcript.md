# 021 Tool calls are written ahead to the transcript

Status: accepted (P54)

## Context
A run wrote its messages to the transcript once, at its end. If anything failed after a tool ran
(recording the batch in `tool_executions`, the checkpoint, the session save) the failed run kept
only the user's message: the assistant message with the calls was dropped because its results were
not in the list yet. A crash (kill, power loss) kept nothing at all. Either way the next turn's
model had no trace that the tool ran, so "try again" ran the mutating tool a second time. The
P54 smoke shows it with a real model: `echo hello >> log.txt` ended with two lines.

The known limit spoke of "a retry with the same run id", and the brief asked for a write-ahead row
per `tool_call_id` in `tool_executions`. Reading the code: no path re-runs a run id.
`RunManager` mints a new id per run and `recover()` marks unfinished runs failed, never re-runs
them. A retry is always a new run, and a real model mints new tool call ids, so a skip keyed on
`(run_id, tool_call_id)` never fires for it. P51's model retries re-ask the model only, never
the tools.

## Decision
- **The transcript is the write-ahead log** (it is the source of truth, ADR 008). Right before a
  tool batch runs, the run's unsaved messages (the user's message, the assistant's calls) are
  appended; right after, the results are appended, before anything is recorded in SQLite. The end
  of a run and the failure path append only what is still unsaved.
- **A call with no result reads as "outcome unknown".** When a run loads its history (and on
  `/compact`), any tool call without a result (its run died while it ran) gets a stand-in result,
  in memory only, right after the call's other results: "outcome unknown ... It was not run
  again: check its effect before repeating it." The pair stays valid for the provider and the
  model knows the side effect may have happened. It never re-runs the call itself.
- No write-ahead row in `tool_executions` and no new store method: they would guard a path no
  gateway has. The existing skip stays for direct `AgentRuntime.run(run_id=...)` callers.

## Consequences
- A failed or killed run keeps its calls and results; the next turn's model checks instead of
  repeating (smoke: one line in both cases, a model that read the file before answering).
- A cancelled or timed-out run keeps the call that was running (before, it was dropped). The
  subagent wrap-up now says such a call "may have partly run", not "not run".
- A run writes the transcript a few times instead of once, each line tagged with its run id
  (`append_messages(..., run_id)`, `HistoryItem.run_id`). The web, opening a chat whose run is
  still going, drops that run's rows except its opening message, since the run's replayed events
  render the rest (the TUI keeps a running chat in its tab and never reloads it). The legacy transcript-only rewind counts a turn's file
  changes up to the turn's last line (its first line is now written early).
- Read-only and mutating tools are treated the same: nothing re-runs automatically, the model
  decides.
