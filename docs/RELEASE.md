# Before making the repository public

Checked on 2026-11-24 against commit `HEAD` of `main`. "Done" means verified, not assumed.
History was rewritten twice since (2026-11-25: every commit authored as `aayushjoshi-12`, no AI
trailers; 2026-11-26: `.claude/skills/` removed from every commit). Neither added files, so the
history checks below still hold; re-run them before publishing anyway.

## Done

- [x] No secrets in the current tree: a scan for OpenRouter, OpenAI-style, Telegram, GitHub and
      AWS key shapes finds only the obviously fake values in tests.
- [x] No secrets in history: no commit ever added an `sk-or-...` string, a `.env` or a database.
- [x] `.env`, `.env.*` (except `.env.example`), `agent.db`, `tmp/` and the agent workspace are
      gitignored and not tracked.
- [x] `make check` (ruff, mypy, pytest) passes.
- [x] Safety limits are documented: the shell guard is not a sandbox, and unattended runs belong
      in a container (`docs/BENCHMARKS.md`, "Safety rules").

## Open: needs a decision from the owner

- [ ] **Licence.** There is no `LICENSE` file and no `license` field in `pyproject.toml`. Without
      one, the code is "all rights reserved" even when public. Pick one (MIT or Apache-2.0 are
      the usual choices) and add both.
- [ ] **Telegram.** Revoke the bot token that was used during development (BotFather, `/revoke`)
      and issue a new one; set `/setjoingroups` to Disable if the bot should only answer you.
- [x] **Package metadata.** `description` in `pyproject.toml` is set (checked 2026-11-26).
- [ ] **Personal paths and names** in `docs/` and `STATUS.md`: read once for anything private
      (project names, home directories, chat IDs) before publishing.

## Open: needs a real terminal

- [x] Run `agent-stack tui` and approve, deny and cancel a gated tool call by hand (2026-11-25).
- [ ] Run `agent-stack telegram` and do the same through the inline approval buttons.

## Worth stating in the README

- `agent-stack run --yes` approves every gated tool call; only use it inside a container.
- The benchmark numbers are small samples on a cheap model (`docs/BENCHMARKS.md`).
