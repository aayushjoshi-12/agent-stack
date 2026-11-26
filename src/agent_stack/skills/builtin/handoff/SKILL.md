---
name: handoff
description: Write a handoff file that lets a fresh conversation with an empty context pick up this one's work cold. Use when the user says "handoff", "write a handoff", "I'll continue in a new chat", or the conversation is long and they want to switch. /handoff [what the next chat should do]
---

# Handoff

A handoff is a context transplant. Its one reader is a brand-new conversation that has none of
this one's messages, about to continue the work. Write for that reader: everything they need to
carry on without asking the user again or re-deriving what was already learned, and nothing they
do not.

## Gather before you write

- Re-read this conversation from its start. If earlier messages were replaced by a summary, use
  that summary; for detail it lost, `ask_chat` or `read_chat` on this conversation (find its id
  with `find_chats`).
- Check the live state rather than trusting memory: `git status` and `git log --oneline -15`
  (when the folder is a git repository), your todo list, the files you changed, and test results.
- Read any handoff, plan or ticket files this conversation started from, and say which ones the
  next chat should open.
- Call `reference_here` and put the reference in the file, so the next chat can open or fork this
  exact point if it needs the full transcript.

## Where it goes

`tmp/handoff/<YYYY-MM-DD>-<short-kebab-title>.md` in the workspace, unless the user names a
path. Before writing, check that `tmp/` is gitignored; if it is not, add a `tmp/` line to
`.gitignore` so scratch files never reach a commit. Never commit or push the handoff.

## What it contains, in this order

1. `# Handoff: <what this conversation did>`, then one line: date written, workspace path, and
   the conversation reference.
2. **Request and intent.** Everything the user asked for, in their words where it matters, and
   the rules they set for how to work (things to avoid, accounts to use, budgets, approvals).
3. **Done.** What is finished and its exact state: implemented, tested, committed (hashes),
   pushed or not.
4. **In progress.** What was being worked on right before this handoff, precisely enough to
   resume mid-step: the file, the function, the failing test.
5. **Next step.** The single next action, and any steps after it. Mark which ones the user asked
   for and which ones you are suggesting.
6. **Key facts.** Paths, commands, versions, names, config values and identifiers the next chat
   will need verbatim.
7. **Files.** Each file read or changed that matters, with what changed and why, and patterns or
   files to copy instead of re-deriving.
8. **Errors and fixes.** What went wrong, how it was fixed, and what is still broken.
9. **Decisions.** Choices made and the reason for each, so the next chat does not reopen them.
10. **Do not.** Sharp edges and anti-scope: work already done elsewhere, actions that need the
    user's OK, things that look in scope and are not.
11. **User messages.** Every message the user wrote in this conversation, in order, quoted or
    closely paraphrased. They carry intent the summary sections lose.
12. **Start the next chat with.** A ready-to-paste first message, for example:
    `Read tmp/handoff/<file>.md and continue from "Next step". <anything else it must know>`

Skip a section only when it is empty. Keep every exact identifier. Prefer facts over narrative.
Never write secrets (API keys, tokens, passwords) into the file; name the variable that holds them.

## After writing

Tell the user the file's path and the ready-to-paste first message. Then stop: the point of a
handoff is a clean start in a new conversation (`/new` in the TUI), not a checkpoint to keep
working past in this one.
