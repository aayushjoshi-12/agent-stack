# ADR 010 — The workspace is the current directory

**Status:** Accepted

## Context

The workspace is the root the agent's file, shell, memory and session tools are scoped
to (`WorkspacePolicy`). It began as a fixed `./workspace/` directory holding runtime data
only. That made the default run useless for real work: the agent could not read the
project it was launched in, and subagents (which share the same policy) reported the
source tree as missing. The runtime data bled into a directory that looked like a project
but was not one.

## Decision

- `AppConfig.workspace` defaults to the **current directory** (`Path.cwd`), so running
  `agent-stack` inside a project opens that project. `AGENT_STACK_WORKSPACE` still
  overrides it.
- The SQLite index lives **inside the workspace** at `<workspace>/agent.db`
  (`AppConfig.database`); `AGENT_STACK_DB` overrides the path. Sessions, memory, skills
  and the audit log already live under `<workspace>/.agent/`.
- `agent-stack` with no arguments launches the TUI — the single-command entry point.
  Subcommands (`tui`, `chat`, `audit tail`) remain.
- The workspace is resolved once, at wiring time (in the composition root), not per run.

## Consequences

- Running the tool in a repo lets the agent (and its subagents) read and act on that
  repo; `.agent/` and `agent.db*` are gitignored so it stays out of version control.
- Instructions (`AGENTS.md`) are discovered by ascending from the workspace, so a run in
  a project's root sees that project's `AGENTS.md`. Running in a subdirectory loads
  instructions for a tree above the workspace — a known, accepted mismatch.
- Pointing `AGENT_STACK_WORKSPACE` at an old `./workspace/` still works; the old session
  transcripts simply stay where they were.
