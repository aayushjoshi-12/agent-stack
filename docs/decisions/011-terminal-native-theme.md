# ADR 011 — The TUI inherits the terminal's theme

**Status:** Partially superseded by [ADR 012](012-tui-revamp.md). The terminal-native `ansi-*` themes remain selectable via `AGENT_STACK_THEME`; the default look, layout and responsiveness changed.

## Context

The first Textual TUI never set a theme, so it ran on Textual's default
`textual-dark` theme — a hardcoded palette that assumed a dark terminal and painted its
own backgrounds, borders and accents. On a light terminal that looked wrong, and even on a
dark one it imposed colours the user had not chosen. Styling was also decorative rather
than semantic: cyan/`green`/`yellow`/`magenta` literals, a boxed border around every pane,
repeated headings, and colour as the only status signal.

## Decision

- The app uses a **terminal-native Textual theme**: `ansi-dark` by default, whose
  `foreground`, `background` and `surface` all resolve to `ansi_default` (the terminal's
  own colours) and whose accents are the terminal's ANSI palette. Nothing in the app's CSS
  or rendering names an RGB/hex colour.
- `AppConfig.theme` (`AGENT_STACK_THEME`) selects the theme. `ansi-light` is the sane
  choice for a light terminal; any registered Textual theme name works. An unknown name is
  ignored and the default is kept.
- **Secondary text recedes with `text-style: dim`**, not with a muted colour:
  `$text-muted` flattens to plain default under ANSI themes, so it cannot express
  hierarchy.
- **Colour is used sparingly, for meaning or accent.** Content is terminal-default; a
  failed run is `bold red`, diff additions/removals/hunks are green/red/cyan, headings and
  the thinking label are bold `ansi_magenta`/yellow, and tool names are green. Status
  always carries a word, never colour alone.
- **Regions are separated by darker panels, not rules or boxes.** The layout is a wide
  conversation column (2fr) and a narrow context sidebar (1fr); the sidebar, the input
  composer, user messages, terminal transcripts and fenced code blocks all sit on the
  `.panel` surface — `ansi_black` on dark terminals, flipping to `ansi_bright_black` under
  `:light` so light themes stay readable. There are no divider lines: whitespace does the
  rest of the separation.
- **Assistant messages render as Markdown** (the Textual `Markdown` widget): headings are
  bold `ansi_magenta`, bold is bold, inline code sits on a darker tint, and fenced code
  blocks get Textual's **ANSI-aware syntax highlighting** — only for generated blocks with
  a language tag. Two `:ansi` defaults are overridden to keep the darker panels
  (`MarkdownFence` and `.code_inline` become transparent under ANSI themes).
- **`run_command` output is a live terminal transcript** inline in the chat. Once the
  stream shows unified-diff markers (`diff --git`, `@@`, file headers), its lines are
  diff-coloured by role; otherwise output stays dim.
- **The app carries no box-drawing noise**: no tree expanders/guides, no `✓`/`✗`/`·`
  marks, no divider lines; the only glyphs are the plain `[+]`/`[-]` collapsible toggles
  and the `→` tool rows. The `Collapsible`'s `hkey` border is removed — it is a divider
  line. Thinking is a collapsed `Collapsible`; user messages sit on a darker panel; the
  audit tail was a debugging view and was removed. The empty conversation is blank — the
  ASCII-art banner is gone.
- **Scrollbars are unobtrusive**: one cell thick, transparent track, muted grey thumb.
- **Responsive collapse.** Textual CSS has no media queries, so the app toggles a `narrow`
  class on the screen from `on_resize` (using `event.size`, since `App.size` is still
  stale inside the handler): `< 100` columns hides the context sidebar. `ctrl+b` forces
  the sidebar back on so it is never permanently unreachable.
- A few Textual ANSI defaults are overridden because they add noise or fail on one
  polarity: the `Collapsible` keyline, the transparent `Markdown` code surfaces, the ANSI
  buttons' tall boxes, and the bright-magenta scrollbar thumb.

## Consequences

- The app looks native next to the user's shell and adapts to their palette; a light
  terminal needs `AGENT_STACK_THEME=ansi-light` because Textual cannot detect terminal
  polarity (a deliberate limit — no probe).
- Some emphasis that colour would have provided is now carried by weight (`bold`) and
  attributes (`dim`), which is less expressive but never fights the terminal theme.
- Syntax highlighting and diff colours are ANSI-standard hues (the terminal's own green,
  red and cyan), so they inherit whatever the user's palette maps them to.
- Widget-specific ANSI defaults (unused widgets, hover states of Textual internals) may
  still show Textual's own ANSI choices; overrides are added only where they were visible.