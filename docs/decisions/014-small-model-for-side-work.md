# ADR 014 — A small model for side work, never for edits

**Status:** accepted (2026-11-14).

## Context

Claude Code runs its main model for the conversation and edits, and a Haiku-class model for
background side-work (session titles, topic detection, small helpers); read-only exploration has
also been pinned to a smaller model, though sources disagree on whether the current built-in
Explore agent still is. `opusplan` is a phase split (Opus plans, Sonnet executes). No source says
a smaller model *writes* the edits. (Checked against Anthropic's model-config docs and
community write-ups, 2026-11-14.)

## Decision

`AGENT_STACK_SMALL_MODEL` names a cheaper model used for two things only:

1. the read-only `explorer` subagent (it only searches and reads; a weak answer costs a retry,
   not a corrupted file), and
2. naming a conversation after its first message (3 to 6 words; the first message stays as the
   title if the call fails or the user renames meanwhile).

Answers and every `write_file`/`edit_file` use the main model: `edit_file` needs exact text and
a weaker model mis-edits more. The small model's spend is a `model.usage` event like any other,
so the cost totals include it and show a per-model split.

Default: on OpenRouter, the cheapest tool-capable model that completed real agent tasks in
testing; on any other provider, no split (a default for a provider the user may have no key for
would break their setup). `main` switches the split off.

## Consequences

- One extra tiny model call per new conversation, and cheaper exploration.
- Not done, by choice: a plan/execute split (`opusplan`), and a model that "applies" edits.
- Found on the way: a run wrote its in-memory session back at the end, undoing a rename made
  during the run; the end-of-run save now keeps the stored title.
