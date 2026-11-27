# 022 Memory suggestions are reviewed, and opt-in

Status: accepted (P53)

## Context
P21 gave the model `remember`/`recall`/`forget` over a per-user fact store: the model decided
alone what to keep. Automatic extraction after each turn would find more worth keeping, but the
turn is untrusted (pasted text, fetched pages echoed in a reply) and "remember X" planted there
must not become a durable fact that every later conversation trusts. It also costs a model call
per run and sends the turn to the side model's provider.

## Decision
- **Opt-in** (`AGENT_STACK_MEMORY_SUGGESTIONS=on`, default off): cost and privacy are the user's
  call. The small model does it (the main model without a split).
- **Suggest, never store.** After a top-level run succeeds, a background task (the same set as
  conversation titles, off the run's path: it cannot delay or fail the run) asks for at most three
  `{key, value}` facts through structured output and saves them as `pending` rows in
  `memory_suggestions`. Code drops empty ones, ones carrying a secret (the redactor would change
  them), ones already stored and ones suggested before in any state.
- **Only the user stores.** The one path into `memory_items` is
  `Application.accept_memory_suggestion`, called by a gateway on a user action and checked against
  the principal's namespace. Rejected rows stay, so the same fact is not offered again.
- **With suggestions on, the model's own memory writes are withdrawn** (`remember` and the file-memory `write_memory_topic`, which the first real web run used to store the same facts unreviewed); `recall`, `forget` and `read_memory_topic` stay.
  Otherwise the model stores the same facts unreviewed (the first real smoke run did exactly that).
- Kept facts reach later conversations through `recall` only; no new resident prompt block.

## Consequences
- A prompt-injected "remember X" can at most appear as a suggestion the user rejects.
- The file memory (`write_memory_topic`, P16d) is still written by the model without review; it is
  meant for project notes, but models put user facts there too (ticketed).
- One extra small-model call per finished run, counted in the conversation's cost
  (`model.usage`, `purpose: memory`).
