# ADR 011 — The TUI inherits the terminal's theme

**Status:** Accepted

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
  failed run is `bold red`, and the terminal's own ANSI accents colour the few things that
  deserve emphasis (header identity, active tab, tool names, banner). Status always carries
  a word, never colour alone.
- **Regions are separated by whitespace and a single draggable rule**, not a box per pane:
  one-cell `Divider` widgets between columns (drag to resize) and one rule above the input.
  Spacing uses a fixed scale (0 · 1 · 2 cells).
- **The app carries no box-drawing noise**: no tree expanders/guides (`show_root=False`,
  `show_guides=False`), no `✓`/`✗`/`·` marks; the only glyphs are the plain `[+]`/`[-]`
  collapsible toggles. Thinking is a collapsed `Collapsible`; user messages sit on a
  `ansi_bright_black` panel so they read as the user's; the audit tail was a debugging view
  and was removed.
- **Scrollbars are unobtrusive**: one cell thick, transparent track, muted grey thumb.
- **Responsive collapse.** Textual CSS has no media queries, so the app toggles `narrow`
  and `tiny` classes on the screen from `on_resize` (using `event.size`, since `App.size`
  is still stale inside the handler): `< 100` columns hides the right-hand panes, `< 68`
  hides the session sidebar. `ctrl+b` forces the panes back on (hiding the sidebar
  instead) so they are never permanently unreachable.
- A few Textual ANSI defaults are overridden because they add noise or fail on one
  polarity: the tabs' full-width underline is hidden (the active tab is accent/bold), the
  ANSI buttons' tall boxes, the tree guides/expanders, the bright-magenta scrollbar thumb,
  and the footer keys (magenta → bold default).

## Consequences

- The app looks native next to the user's shell and adapts to their palette; a light
  terminal needs `AGENT_STACK_THEME=ansi-light` because Textual cannot detect terminal
  polarity (a deliberate limit — no probe).
- Some emphasis that colour would have provided is now carried by weight (`bold`) and
  attributes (`dim`), which is less expressive but never fights the terminal theme.
- Widget-specific ANSI defaults (unused widgets, hover states of Textual internals) may
  still show Textual's own ANSI choices; overrides are added only where they were visible.
