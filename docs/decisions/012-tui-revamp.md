# ADR 012: TUI revamp (partially supersedes ADR 011)

**Status:** Accepted

## Context
ADR 011 made the TUI inherit the terminal's ANSI palette. That avoided clashing with the
user's terminal but left the UI flat: everything was terminal-default text, hierarchy came
only from dim/bold, tool calls and thinking were bare rows, the composer was a single line,
the status bar was hand-rolled text, and responsiveness was one `on_resize` hack hiding the
sidebar. The result read as unfinished.

## Decision
- **Named true-colour themes** (`agent-stack-dark`, `agent-stack-light`) are the default.
  The stylesheet uses theme variables only, so `AGENT_STACK_THEME=ansi-dark` (ADR 011's
  behaviour) and every built-in Textual theme still work. Depth comes from three
  background layers, not borders. This reverses ADR 011's "no hex anywhere" rule in
  favour of a single `theme.py` that owns every literal.
- **State is never colour alone**: spinner/✓/✗ glyphs plus words, duration on tool cards.
- **Tool calls and thinking are collapsible cards** with live status; shell output is capped
  and coalesced so a noisy command cannot stall the UI.
- **Multi-line composer** (Enter sends, Shift+Enter or Ctrl+J newline, Up/Down history).
- **Responsive via Textual breakpoints** (`HORIZONTAL_BREAKPOINTS`, `VERTICAL_BREAKPOINTS`)
  instead of `on_resize`; the sidebar auto-hides below 100 columns, Ctrl+B overrides.
- **Footer + `?` help overlay**: the footer shows the few keys that matter now, the overlay
  lists all of them. Esc always cancels the run; Ctrl+C cancels, then quits.
- **Conversations are first-class**: `ctrl+n` starts a new stored session instead of only
  clearing the screen (which left the model's context intact and the old chat unreachable);
  `ctrl+o` reopens earlier ones. This needed `SessionStore.list_for_owner` and the
  `Application` methods `new_session`, `list_sessions`, `set_session_title`, `session_history`.
- The gateway still touches only `Application`, `gateways.base` and `core.events`.

## Consequences
- Looks designed on any true-colour terminal; ANSI users keep an opt-out.
- More files (`theme.py`, `widgets.py`, `styles.tcss`) but each is small and testable.
