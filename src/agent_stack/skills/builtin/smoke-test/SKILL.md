---
name: smoke-test
description: Run a real manual smoke test of a change, with the real services and the real credentials from the workspace's .env and nothing mocked on the changed path, then write a report. Use when the user says "smoke test", "manually verify", "test it for real" or "with real creds". /smoke-test [what to verify]
---

# Smoke test

A smoke test exercises the real thing: a real database, a real running server, a real external
API, a real model call. It catches what a test suite full of fakes cannot: wrong wiring, a real
service answering in an unexpected shape, an auth or config mistake. It is manual and disposable,
never part of the test suite or CI.

## Decide what to verify

- Take the target from the user's words. Without one, it is the change this conversation made:
  check `git status` and `git diff` (or the files you changed) and name the paths it touched.
- List the checks before writing anything: each one is a real action and the result that proves
  it worked. Cover the changed path end to end, plus one obvious failure case when it is cheap.

## Write the script

- Put it in `tmp/smoke_tests/<YYYY-MM-DD>-<short-title>-smoke.py`, or `.sh` for a pure curl check.
  Check that `tmp/` is gitignored first; if it is not, add a `tmp/` line to `.gitignore`.
- Nothing mocked on the path under test. Stub only what is irrelevant to this check, and say in
  the script's docstring what is real, what is stubbed and why.
- Use the real credentials already in the workspace's `.env` (load them with `python-dotenv` or
  `set -a; . ./.env`). Never print, log or write a secret's value: print whether it is set.
- For an HTTP surface, send real requests to a real running process, or through the real app
  factory, so routing, middleware and auth are part of the test.
- When the path calls a real model, let it: a mocked call cannot catch a real model's behaviour.
  Keep the spend small: the fewest calls that prove the path, with the cheapest model that
  exercises it. Follow any budget or "no benchmarks" rule in AGENTS.md or the handoff.
- The docstring also holds the prerequisites (services to start, migrations to run) and a
  `Usage:` line with the exact command.

## Ask first

Anything that reaches other people or cannot be undone needs the user's explicit OK before you
run it: messaging a real user or bot chat, posting, emailing, pushing, deploying, deleting real
data, or spending noticeably. Shared or company installs stay untouched. Say exactly what will
happen and wait.

## Run it and fix what breaks

Run the script with `run_command`. When a check fails, find the cause, fix the code, and rerun
the whole script until every check passes. Then run the project's normal gate (`make check` or
its test command) too.

## Write the report

`tmp/smoke_tests/<same name>-result.md`, in this order:

1. `# Smoke test: <what> (YYYY-MM-DD)`
2. **Context.** What it ran against, where the credentials came from (the file, never the
   value), the prerequisites, and exactly what was real and what was stubbed.
3. **Script.** Its path, or the literal commands issued.
4. **Results.** A `| Check | Result |` table, then one line: `Overall: ALL PASS` or
   `FAILED: <what>`.
5. **Bugs found and fixed.** Each bug, its cause and its fix, even when already fixed.
6. **Also ran.** The project gate and its result.

Then tell the user the overall result, the bugs found, and the report's path. Do not commit
unless the user asks; when they do, commit only once the smoke test and the gate both pass.
