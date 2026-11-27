# 020 Tool permissions only narrow: skills and subagents

Status: accepted (P49)

## Context
A skill can declare `allowed-tools` in its `SKILL.md`; Claude Code reads that as "tools the
skill may use without asking". A subagent is an `Agent` with its own tools and its own (empty)
`ToolPolicy`, so before P49 a child gated nothing: `explorer` could read outside the workspace
without the approval its parent needed, and a write-capable child would have written unasked.
Skills come from files any cloned repo can carry, and a subagent's task is text the parent model
wrote, so both are reachable by prompt injection.

## Decision
- **`allowed-tools` is a restriction, never a pre-approval.** After `load_skill` loads a skill
  that declares it, the run may call only those tools plus the two skill tools; any other call is
  an error result and never reaches the approval prompt. Listed tools that need approval still
  need it. Several loaded skills intersect. The scope lives on the run's `RunContext`
  (`SkillScope`) and ends with the run. Names must match ours exactly: Claude Code names such as
  `Read` or `Bash(git:*)` match nothing, which blocks (the safe failure).
- **A subagent's permissions are the intersection with its parent's.** `delegate` starts the
  child as `inherit(child, parent, scope)`: only tools the parent has and its active skill
  allows, and a policy that is the union of both (`ToolPolicy.union`), so every call the parent
  would have to approve, by name or by a dynamic rule, needs approval in the child too. It
  composes down any depth because a child's runtime agent is the narrowed one. Approval mode,
  broker and advisor were already shared through the `AgentFactory`.

## Consequences
- The `coder` subagent (read, edit, write, `run_command`) is safe to offer: its writes ask for the
  same approval the parent's would, on the child's run id.
- A child's approval events (`approval.required`/`resolved`/`auto`) are also put on its top-level
  run's handle stream, with the child's run and session ids. The CLI, TUI and Telegram read only
  that stream; before this a child waiting for a human hung there (the web UI already watched
  child runs, and reads the bus, so it sees no duplicate).
- A vendored Claude Code skill with `allowed-tools: Read Grep` can call no real tool here until an
  alias map exists.
