# TUI design conventions

> The rules the Textual TUI (`gateways/tui/`) is built to. The decision is
> [ADR 012](../decisions/012-tui-revamp.md) (it partly supersedes
> [ADR 011](../decisions/011-terminal-native-theme.md)); this is the working reference.

## Principles

1. **Roles, not colours.** The stylesheet (`styles.tcss`) and markup only name theme roles
   (`$primary`, `$accent`, `$success`, `$error`, `$foreground-muted`, `$surface`, `$panel`).
   Every literal colour lives in `theme.py`, so any theme restyles the whole app.
2. **Depth from layers, not borders.** `$background < $surface < $panel`. Borders appear only
   as a thick left accent bar (user message, composer, errors) and on the help overlay.
3. **State is never colour alone.** Spinner / ✓ / ✗ glyphs plus words and durations.
4. **Never block, always escapable.** Agent work runs in tasks; `esc` always cancels a live run
   or returns focus to the prompt.
5. **Fixed positions.** Header, conversation, composer, sidebar and footer never move.

## Themes

`agent-stack-dark` (default) and `agent-stack-light` are true-colour. `AGENT_STACK_THEME`
accepts them, the terminal-native `ansi-dark` / `ansi-light`, or any built-in Textual theme;
an unknown name falls back to the default. `ctrl+t` toggles dark/light; the command palette
lists every theme.

## Layout

```
HeaderBar   ◆ agent-stack · task title ......... run state · model        height 1
#body       conversation (1fr)                  │ sidebar (34 / 40 wide)
              #chat  (VerticalScroll)           │   SESSION · CONTEXT meter
              #composer (multi-line)            │   ACTIVITY · SUBAGENTS
              #hints  model · key hint          │   WORKSPACE (path, branch)
Footer      esc cancel · ^n new · ^b sidebar · f1 help ........ ^p palette
```

## Responsive

Textual breakpoints put classes on the screen (`HORIZONTAL_BREAKPOINTS`,
`VERTICAL_BREAKPOINTS` on `TuiApp`); `styles.tcss` reacts to them:

| Class | When | Effect |
|---|---|---|
| `-narrow` | < 100 cols | sidebar and model name hidden, tighter gutters |
| `-normal` / `-wide` | 100 to 139 / 140+ | sidebar 34 / 40 wide |
| `-short` | < 24 rows | hints, welcome and row spacing dropped |
| `-show-sidebar` / `-hide-sidebar` | `ctrl+b` | explicit override of the width default |

## Conversation rows (`widgets.py`)

| Row | Widget | Treatment |
|---|---|---|
| user message | `UserMessage` | `$panel` surface with a `$primary` accent bar |
| assistant message | `AgentMessage` (Markdown) | headings coloured by level; inline code on `$panel`; fences on `$surface` |
| thinking | `Disclosure("think")` | collapsed, italic muted; toggles with Enter / click |
| tool call | `Disclosure("tool")` | header `chevron · spinner/✓/✗ · name · detail · duration`; `run_command` opens while running |
| tool output | body of the card | last 10 lines while running, last 60 when opened later; 400 lines kept, older ones counted as "earlier lines hidden"; diff-coloured when it looks like a unified diff |
| subagent | `SubagentRow` | one row, updated in place: running spinner then ✓ done |
| failure | `Notice(kind="error")` | `$error-muted` surface with an error bar |

A successful run adds no row. Finished cards fold when their output is longer than 6 lines
(failed cards stay open); a card the user toggled keeps the user's choice. Child-run detail
events stay out of the parent conversation.

## Composer

`PromptArea` is a multi-line `TextArea` (1 to 8 lines). `enter` sends; `ctrl+j`, `shift+enter`
or `alt+enter` inserts a newline; `up`/`down` walk the sent history from the first/last line;
`pageup`/`pagedown` scroll the conversation. While a run is active, submitting is refused with a
notification and the text is kept.

## Header and sidebar

The header shows the run state (`● idle`, `⠋ working 12s`, `◌ cancelling…`, `✓ done 12s`,
`■ cancelled`, `✗ failed`). The sidebar shows the session, an **estimated** context meter
(about 4 chars per token against a 200K window; amber at 70%, red at 90%), tool-call count,
the last four subagents, and the workspace with the git branch.

## Keys

`enter` send · `shift+enter`/`ctrl+j` newline · `esc` cancel run or focus prompt ·
`ctrl+c` cancel, then quit · `ctrl+q` quit · `ctrl+n` new conversation · `ctrl+b` sidebar ·
`ctrl+t` theme · `ctrl+p` palette · `f1` help overlay.

## Performance

Output is coalesced: cards mark themselves stale and a 10 Hz tick repaints them, so a fast
stream costs one repaint per tick, not per chunk. The chat is anchored to the bottom only once
a conversation exists (an anchored container bottom-aligns short content).
