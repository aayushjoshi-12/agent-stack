# TUI design conventions

> The rules the Textual TUI (`gateways/tui/`) is built to. The theming *decision* is
> [ADR 011](../decisions/011-terminal-native-theme.md); this is the working reference.

## Principle

The TUI is a **terminal citizen**: it borrows the user's colours and spacing conventions
rather than importing a visual identity. It has no palette of its own and never paints a
background except for the one deliberate surface below. It should look native whether the
terminal is dark, light or custom — legibility is the terminal's job, not ours.

## Layout

Three regions, separated by **whitespace and a single draggable rule, never boxes**:

```
header      identity (left, accent bold) · runtime metadata (right, dim)   height 1
body        left sidebar (runs) │ conversation (1fr) │ panes (subagents/shells)
prompt      one rule above the input, no border around it
footer      key hints
```

- Spacing uses a fixed scale: `0 · 1 cell · 2 cells` (padding), and `margin-top: 1`
  between conversation rows. Panes are padded (`1 2` / `1 3`), never boxed.
- The vertical rules are **1-cell `Divider` widgets**, not borders. Drag one to resize
  the pane beside it (left sidebar and right panes are both resizable); it highlights on
  hover. Hiding a pane also hides its divider (`narrow` / `tiny` classes).

## Conversation

The chat is a `VerticalScroll` of message widgets, not a raw `RichLog`, so rows can differ:

| Row | Treatment |
|---|---|
| user message | `.msg-user` — a panel (`ansi_bright_black` background, padded) so it reads as *yours* |
| assistant message | plain terminal-default text |
| thinking | a `Collapsible` (`.msg-think`) titled `thinking`, collapsed by default; `[+]` / `[-]` toggles, body is dim italic |
| tool call | `.tool-line` — the tool name (green) plus the command for `run_command` (dim) |
| subagent | `.subagent-line` — `name  started` / `name  done` |
| failure | `.msg-error` — the one alarming tone (`bold red`) |

A successful run emits **no chat line** — there is no "run completed" noise. Tool calls are
announced (with the bash command when there is one) but their completion is left to the
Shells pane. The audit tail was removed from the app: it was a debugging view, not a UI.

The empty conversation shows an ASCII wordmark banner (`welcome_block`), removed on the
first message.

## Text

Styling is **semantic, not decorative** (`render.py`):

| Verb | Style |
|---|---|
| identity / headings / active tab | `$accent` (terminal's own accent), bold |
| tool / subagent names | green (ANSI standard) |
| secondary (commands, status words, pane titles, footer descriptions) | dim |
| the model's working (thinking body) | dim italic |
| failure | **bold red** — the only alarming tone |

There are **no box-drawing glyphs** in the app: no `▼`/`►` expanders (trees hide their root
and set `show_guides=False`, `guide_depth=2` so leaves sit at column zero), no `├──`
guides, no `✓`/`✗`/`·` status marks, and the collapsible toggle is plain `[+]` / `[-]`.
Status is carried by words.

Colour is used sparingly as an accent: the header identity, the active tab, tool names,
the banner, and the divider on hover. It never carries meaning on its own.

## Scrollbars

One cell thick (`scrollbar-size-vertical: 1`) with a transparent track
(`scrollbar-background: transparent`) and a muted `ansi_bright_black` thumb that brightens
to `$accent` on hover/drag.

## Responsive collapse

Textual CSS has no media queries, so `_apply_responsive` toggles `narrow`/`tiny` classes on
the screen from `on_resize` (using `event.size`, since `App.size` is stale inside the
handler):

- `< 100` columns → hide the right-hand panes (keep the conversation readable)
- `< 68` columns → also hide the session sidebar
- `ctrl+b` → force the panes on (hiding the sidebar instead), so panes are never
  permanently unreachable

## Keys

`Enter` send · `Ctrl+C` cancel a run, or quit when idle · `Ctrl+Q` quit ·
`Tab`/`Shift+Tab` cycle panes · `Ctrl+P` command palette · `Ctrl+B` toggle panes.

## Overriding Textual's ANSI defaults

Textual's `*:ansi` styles sometimes add noise or fail on one polarity. Overridden in
`app.py` CSS, each with a comment:

- `Tabs` full-width `Underline` bar → hidden; the active tab is merely bold/accent.
- tree guides and expanders → off (flat, unguided lists).
- footer keys (magenta) → bold terminal-default text; descriptions dim.
- `Button` — Textual's ANSI flat/default buttons are tall boxes → 1-line flat text.
- scrollbars — Textual's bright-magenta thumb → muted grey (see above).
