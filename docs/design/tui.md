# TUI design conventions

> The rules the Textual TUI (`gateways/tui/`) is built to. The theming *decision* is
> [ADR 011](../decisions/011-terminal-native-theme.md); this is the working reference.

## Principle

The TUI is a **terminal citizen**: it borrows the user's colours and spacing conventions
rather than importing a visual identity. It has no palette of its own and never paints a
background except for the one deliberate surface below. It should look native whether the
terminal is dark, light or custom — legibility is the terminal's job, not ours.

## Layout

Two columns, separated by **panel colour, never a rule or box**:

```
conv-title  bold task title (the first user message) · "+" (new conversation)   height 1
main        conversation (2fr) │ context sidebar (1fr)
composer    darker panel with a 1-cell input accent on its left edge
status      path · run/session · context usage · ctrl+p commands                height 1
```

- The conversation column holds the chat (`VerticalScroll`), the composer, then the
  status bar. The sidebar holds the task title, a `Context` heading, an estimated token
  count and percentage, empty space, and its own path at the bottom.
- Every region that needs separation sits on a **slightly darker panel**: the sidebar,
  the composer, user messages, terminal transcripts, and fenced code blocks. The `.panel`
  class is `ansi_black` on dark terminals and flips to `ansi_bright_black` (`:light`) so
  light themes stay readable. There are **no divider lines**; whitespace does the rest.
- Spacing uses a fixed scale: `0 · 1 cell · 2 cells` (padding), and `margin-bottom: 1`
  between conversation rows.

## Conversation

The chat is a `VerticalScroll` of message widgets, not a raw `RichLog`, so rows can differ:

| Row | Treatment |
|---|---|
| user message | `.msg-user panel` — a darker panel so it reads as *yours* |
| assistant message | a Textual `Markdown` widget: headings are bold `ansi_magenta`, bold is bold, inline code sits on a darker tint |
| fenced code | syntax-highlighted with Textual's ANSI-aware fence themes (only generated blocks: no language tag ⇒ no highlight) |
| thinking | a `Collapsible` titled `Thinking` (yellow), collapsed by default; `[+]` / `[-]` toggles, body is dim italic. The widget's own `hkey` rule is removed — it is a divider line |
| tool call | `.tool-line` — `→ <tool>  <command-or-arg>` (tool name green) |
| `run_command` | a `.cmd-block panel` — an inline terminal transcript: the `→ run_command  <cmd>` line, then output. Output is diff-coloured once it shows `diff --git`/`@@` markers: headers dim bold, hunks cyan, `+` green, `-` red; otherwise it stays dim |
| subagent | `.subagent-line` — `→ name  started` / `→ name  done` |
| failure | `.msg-error` — the one alarming tone (`bold red`) |

A successful run emits **no chat line** — there is no "run completed" noise. Child-run
detail events stay out of the parent conversation; subagents appear only as their two
lifecycle rows. The empty conversation starts blank: no banner, no ASCII art.

## Task title, sidebar, status

- The first user message becomes the **task title**, shown in the conversation title row
  (truncated) and the sidebar (wrapped). `+` or the palette's *New conversation* resets it.
- The sidebar `Context` section shows an **estimated** token count (≈ 4 chars/token across
  everything written to the chat, capped at a 200K window) and the percentage used; the
  status bar repeats it as `12.3K (6%)`.
- The status bar shows the workspace path (with the current `git` branch when available),
  the active run id, the context usage, and the `ctrl+p commands` hint. The sidebar has
  its own path line at its bottom.

## Text

Styling is **semantic, not decorative** (`render.py`):

| Verb | Style |
|---|---|
| headings | bold `ansi_magenta` (ANSI standard, so it is the terminal's purple/lavender) |
| tool / subagent names | green (ANSI standard) |
| secondary (commands, status words, tokens, output) | dim |
| the model's working (thinking body) | dim italic |
| diff additions / removals / hunks | green / red / cyan (ANSI standard) |
| failure | **bold red** — the only alarming tone |

There are **no box-drawing glyphs** in the app: no tree guides or expanders, no
`✓`/`✗`/`·` status marks, no divider lines. The collapsible toggle is plain `[+]` / `[-]`,
and tool rows use the reference's `→` arrow. Status is carried by words.

Colour is used sparingly as an accent: headings, tool names, the thinking label, the
diff roles, and the status-bar context. It never carries meaning on its own.

## Scrollbars

One cell thick (`scrollbar-size-vertical: 1`) with a transparent track
(`scrollbar-background: transparent`) and a muted `ansi_bright_black` thumb that brightens
to `$accent` on hover/drag. Only the chat scrolls.

## Responsive collapse

Textual CSS has no media queries, so `_apply_responsive` toggles a `narrow` class on the
screen from `on_resize` (using `event.size`, since `App.size` is stale inside the
handler):

- `< 100` columns → hide the context sidebar (keep the conversation readable)
- `ctrl+b` → force the sidebar back on, overriding the width heuristic

## Keys

`Enter` send · `Ctrl+C` cancel a run, or quit when idle · `Ctrl+Q` quit ·
`Ctrl+P` command palette (cancel run, new conversation, interrupt last subagent, focus
prompt, quit) · `Ctrl+B` toggle the sidebar.

## Overriding Textual's ANSI defaults

Textual's `*:ansi` styles sometimes add noise or fail on one polarity. Overridden in
`app.py` CSS, each with a comment:

- `Collapsible` `hkey` border-top → removed (a divider line).
- `Markdown` inline code / fences: the `:ansi` defaults make them transparent → darker
  panels (`ansi_black`, flipped by `:light`).
- `Button` — Textual's ANSI flat/default buttons are tall boxes → 1-line flat text.
- scrollbars — Textual's bright-magenta thumb → muted grey (see above).