# TUI design conventions

> The rules the Textual TUI (`gateways/tui/`) is built to. The decision is
> [ADR 012](../decisions/012-tui-revamp.md) (it partly supersedes
> [ADR 011](../decisions/011-terminal-native-theme.md)); this is the working reference.

## Principles

1. **Roles, not colours.** The stylesheet (`styles.tcss`) and markup only name theme roles
   (`$primary`, `$accent`, `$success`, `$error`, `$foreground-muted`, `$surface`, `$panel`).
   Every literal colour lives in `theme.py`, so any theme restyles the whole app.
2. **Depth from layers, not borders.** `$background < $surface < $panel`. The only edges are a
   thick left accent bar (user message, composer, failed cards, errors and the three dialogs).
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
TabBar      [ ⠋ First task tit... ] [ Second ] [ + ]                     height 3
#body       conversation (1fr)                  │ sidebar (36 / 42 wide)
              #chats (one Conversation per tab) │   SESSION · CONTEXT meter
              #composer (multi-line)            │   ACTIVITY (status, tools, spend) · SUBAGENTS
                                                │   MODEL · APPROVALS · WORKSPACE (path, branch)
Footer      esc cancel · ^n new tab · ^b sidebar · f1 help ...... ^p palette
```

## Responsive

Textual breakpoints put classes on the screen (`HORIZONTAL_BREAKPOINTS`,
`VERTICAL_BREAKPOINTS` on `TuiApp`); `styles.tcss` reacts to them:

| Class | When | Effect |
|---|---|---|
| `-narrow` | < 100 cols | sidebar hidden, narrower tabs, tighter gutters |
| `-normal` / `-wide` | 100 to 139 / 140+ | sidebar 36 / 42 wide |
| `-short` | < 24 rows | one-row tab bar; welcome and row spacing dropped |
| `-show-sidebar` / `-hide-sidebar` | `ctrl+b` | explicit override of the width default |

## Conversation rows (`rows.py`)

| Row | Widget | Treatment |
|---|---|---|
| user message | `UserMessage` | `$panel` surface with a `$primary` accent bar |
| assistant message | `AgentMessage` (Markdown) | headings coloured by level; inline code on `$panel`; fences on `$surface` |
| thinking | `Disclosure("think")` | collapsed, italic muted; toggles with Enter / click |
| tool call | `Disclosure("tool")` | header `chevron · spinner/✓/✗ · name · detail · duration`; `run_command` opens while running |
| tool output | body of the card | last 10 lines while running, last 60 when opened later; 400 lines kept, older ones counted as "earlier lines hidden"; diff-coloured when it looks like a unified diff |
| subagent | `SubagentRow` | one row, updated in place: running spinner then ✓ done |
| failure | `Notice` | `$error-muted` surface with an error bar |

A successful run adds no row. Finished cards fold when their output is longer than 6 lines
(failed cards stay open); a card the user toggled keeps the user's choice. Child-run detail
events stay out of the parent conversation.

## Composer

`PromptArea` is a multi-line `TextArea` (1 to 8 lines). `enter` sends; `ctrl+j` (works in every terminal), `shift+enter`
or `alt+enter` (modern terminals) inserts a newline; `up`/`down` walk the sent history from the first/last line;
`pageup`/`pagedown` scroll the conversation. While a run is active, a plain message is queued
into it (`send_input`): it shows dimmed as "queued" and moves into the conversation when the model
reads it, before its next step or, if it came during the final step, by the run continuing. `up` on
an empty prompt takes the last queued message back (`withdraw_input`) to edit and resend, or to
clear; once the model has read it, it cannot be taken back. If the run is stopped first, unread
messages go back into the prompt. Built-in `/commands` work during a run; a skill waits for it. `/export
[md|json] [path]` writes the conversation into the workspace (default
`<slug-of-title>-<YYYY-MM-DD>.md`, `-2` added rather than overwrite) and notifies the path.

**Images (P44).** Typing `@path/to/shot.png` (relative to the working folder) or pasting or
dragging an absolute path (`/Users/me/My\ Shot.png`, or quoted) attaches that PNG, JPEG, WebP
or GIF (up to 5 MB): a muted `image: shot.png` chip shows under the prompt while it is there, and
the token is taken out of the text on send. `/image <path>` puts `@path` in the prompt. The user
row shows `[image: shot.png]`; a reopened conversation shows the stored name. Only existing image
files count, so `@explorer do x` delegation and plain file names in a sentence are untouched.
While a run is active, a message with an image is not queued (queued input is text only): send
it after the run.

## Conversations

A launch always opens a **fresh draft**, never the last chat. Nothing is stored until the first
message is sent (`Application.new_session` then runs), so opening and quitting leaves no empty
conversation behind.

Each open conversation is a **tab** (`tabs.py`): a `Conversation` pane that holds its own run
state, so a run keeps going while another tab is shown; its events go to the pane that started it.
`ctrl+n` (or `+`, or `/new`) opens a new tab with a fresh draft; on an empty tab it does nothing.
`alt+1`..`alt+9`, `ctrl+pageup`/`ctrl+pagedown` or a click switch tabs; `/close` closes one (not
while it runs; the conversation stays stored). Each tab keeps its unsent prompt text. A background
tab that needs an approval comes to the front with the question. The tab label is the title cut
to 24 columns with `...`, behind a spinner while it runs or `?` while it waits for you.

`ctrl+o` (or the palette's *Switch conversation…*) opens `SessionPicker`: the principal's
sessions, most recent first, the current one marked. Enter shows the conversation's tab if it is
open, else replays the stored history (user text, assistant text, tool calls with their arguments)
into an empty tab or a new one; `d` then `d` again deletes the highlighted conversation for good
(moving the highlight cancels the first press). Deleting an open one closes its tab (or empties
it, if it is the last); a running one is refused. The first message is saved as the title until
the small model names the conversation; the tab picks that name up when the first run ends. Reasoning and tool output are not
stored, so a replayed conversation shows neither. Data crosses the boundary as gateway-neutral
`SessionSummary` and `HistoryItem` (in `gateways/base.py`); the TUI never touches the store.

## Approvals

When a gated tool call needs a decision (`approval.required`, from the parent run or a child), the
card shows `? awaiting approval`, the sidebar says `needs your approval`, and `ApprovalScreen`
opens with the tool and its command or arguments. `y` approves; `n` or `esc` rejects; Enter does
nothing, so a stray key cannot approve a command. The answer goes through
`Application.decide_approval`. If the run ends while its dialog is open, the dialog closes. The set
of gated tools is `AGENT_STACK_APPROVAL_TOOLS`.

## Tab bar and sidebar

The tab bar holds only the tabs. The sidebar shows, for the tab on screen: the session; one
context meter against `AGENT_STACK_CONTEXT_WINDOW` (default 1M for the default model; amber at
70%, red at 90%), which is the input size the model reported for the conversation's last main call
(subagent and title calls excluded), or a `~` estimate (about 4 chars per token) before the first
report; activity (run state `⠋ working 12s`, `cancelling…`, `needs your approval`,
`✓ done in 12s`, `■ cancelled`, `✗ failed`; tool-call count; spend); the last four subagents.
At the bottom: the model, the approval mode (`auto` in amber, else `ask`) and the workspace with
the git branch.

## Keys

`enter` send · `shift+enter`/`ctrl+j` newline · `esc` cancel run or focus prompt ·
`ctrl+c` cancel, then quit · `ctrl+q` quit · `ctrl+n` new tab · `alt+1`..`9` go to tab ·
`ctrl+pageup`/`ctrl+pagedown` previous / next tab · `ctrl+o` switch conversation · `ctrl+b` sidebar (a second press returns to automatic) ·
`ctrl+t` theme · `ctrl+p` palette · `f1` help overlay.

## Performance

Output is coalesced: cards mark themselves stale and a 10 Hz tick repaints them, so a fast
stream costs one repaint per tick, not per chunk. The chat is anchored to the bottom only once
a conversation exists (an anchored container bottom-aligns short content).

## Module map

`gateways/tui/` since P34. `TuiApp` is `app.py` plus one mixin per concern; each mixin lists
what it uses from the rest of the app in an `if TYPE_CHECKING:` block at the top of its class.

| module | holds |
|---|---|
| `app.py` | `TuiApp` (layout, bindings, keyboard focus, cancel / esc / toggles / help) and `TuiCommands` (palette) |
| `runs.py` | `RunsMixin`: start a run, read its stream, route each event into the `Conversation` that started it; approvals, tool cards, subagent rows, end of a run |
| `sessions.py` | `SessionsMixin`: tabs (new, close, switch, tab-bar click) and stored conversations (picker, open, delete, resume) |
| `commands.py` | `CommandsMixin`: what Enter does with the prompt, queued messages, `/` commands and their completion list, approval mode |
| `timeline.py` | `TimelineMixin`: rewind, branches, references, restoring files, the changed-files screen |
| `status.py` | `StatusMixin`: the tick, tab-bar labels, sidebar state, prompt placeholder |
| `tabs.py` | `Conversation` (one tab's pane and run state) and `TabBar` |
| `rows.py` | conversation rows: `UserMessage`, `AgentMessage`, `Notice`, `SubagentRow`, `Disclosure` |
| `chrome.py` | `PromptArea`, `Sidebar` / `SidebarState`, `Welcome`, `HelpScreen` |
| `screens.py` | modal screens: `ApprovalScreen`, `SessionPicker`, `RewindScreen`, `ChangesScreen`, `BranchesScreen` |
| `render.py` | Textual-free text helpers (shared with the line REPL) |
| `theme.py`, `styles.tcss` | the themes and the stylesheet |
