# Benchmarks

What has been measured on agent-stack, how, and what the numbers do **not** show. Every row
says which commit, model and settings produced it. Small samples are labelled as such; nothing
here is comparable to a public leaderboard unless it says so.

Spend ceiling for the whole programme: **US$2** (real OpenRouter calls). Tooling lives in
`bench/` (the Harbor adapter) and the gitignored `tmp/bench/` (scratch drivers and the cost
ledger).

## How runs are done

| Piece | Choice |
|---|---|
| Agent | `agent-stack run --yes -` (one-shot, headless), all tools approved |
| Default model | `openrouter:z-ai/glm-5.3-flash` (cheapest model comparable to Sonnet-class that was tried) |
| Context | `AGENT_STACK_CONTEXT_BUDGET` 24k (Exercism, rag) / 60k (Terminal-Bench): small on purpose, so clearing and summarising actually run |
| Step limit | `AGENT_STACK_MAX_STEPS` 50 (Exercism, rag) / 120 (Terminal-Bench) |
| Grading | Never by the agent. Exercism: the exercise's own tests, run by the harness after the agent stops. Terminal-Bench: the task's verifier (`tests/test.sh`), run by Harbor |

### Safety rules learned the hard way

- The agent's shell (`run_command`) is **not** confined to the workspace. Unattended runs
  therefore go in a container (Terminal-Bench) or a throw-away scratch copy whose path the driver
  checks (`tmp/bench/harness.py` refuses a workspace that is not an existing scratch folder).
- An early run with a mistyped workspace path made the agent wander the filesystem and edit a real
  repository. Nothing was committed, but it is why the check above exists.
- Harbor is installed in its own virtualenv and run with `env -i`, a throw-away `HOME`, and an
  empty `DOCKER_CONFIG`, so it can share no credentials or config with any other Harbor install.

## 1. Exercism Python (the Python slice of Aider's "polyglot" benchmark)

Public task set: <https://github.com/exercism/python> `exercises/practice`. Each exercise is
copied to a scratch folder; the agent gets the stub, the instructions and the test file, and is
told to make `python3 -m unittest discover -p '*_test.py'` pass without editing the tests.
After it stops, the harness runs the tests itself.

Model `z-ai/glm-5.3-flash`, context budget 24k, commit `80ffa36`+.

| Exercise | Passed | Cost (USD) | Model calls | Peak input tokens |
|---|---|---|---|---|
| two-bucket | yes | 0.0024 | 7 | 6,727 |
| forth | yes | 0.0096 | 8 | 12,575 |
| poker | yes | 0.0078 | 8 | 10,702 |
| react | yes | 0.0091 | 11 | 13,009 |
| sgf-parsing | yes | 0.0065 | 7 | 13,774 |
| zipper | yes | 0.0051 | 7 | 8,484 |
| pov | yes | 0.0025 | 7 | 6,877 |
| dominoes | yes | 0.0186 | 12 | 36,722 |
| connect | yes | (in ledger) | | |
| book-store | yes | (in ledger) | | |

**10 / 10, about US$0.08 in total.**

Caveats: 10 of the 34 exercises Aider uses; chosen from its list, not at random; one attempt each;
the agent could run the tests itself (Aider's leaderboard allows a second attempt after seeing test
output, so this is roughly comparable to its "pass@2"). A frontier model would also pass these, so
this shows the harness works end to end, not that the model is strong.

## 2. Real feature in a real repo (rag-pipeline)

Task: add an admin-only `DELETE /traces?older_than_days=N`, with a new permission, tests, the
OpenAPI snapshot and a changelog entry, following the repo's own `AGENTS.md`.

This run was meant for a scratch clone but, because of the mistyped path described above, ran
against the real working tree. It is kept as a data point, not a clean benchmark:

- 52 model calls, 75 tool calls, 190 s, US$0.098, context budget 24k, peak input 35k tokens.
- It read `AGENTS.md`, followed the conventions (permission enum, router, schema, regenerated
  `docs/openapi.json`), wrote tests that passed, and then hit the 50-step limit before the
  changelog/docs. The limit is now configurable (`AGENT_STACK_MAX_STEPS`).
- Context management: at a 24k budget the run never exceeded 35k tokens in a single call, i.e. old
  tool output was being cleared as designed; no summary was needed (`summarizer_calls: 0`).

## 3. Terminal-Bench 2.0 (Harbor)

The benchmark the Claude Code, Codex CLI and similar agent leaderboards use. Official harness:
Harbor (<https://harborframework.com>, <https://www.tbench.ai/docs>). Each task is a Docker image
with its own verifier; the score is the fraction of tasks whose verifier writes reward 1.

Adapter: `bench/harbor_agent.py` (installs agent-stack from `git archive HEAD` into the task
container and runs `agent-stack run --yes -` there).

```bash
# one-time: isolated Harbor (shares nothing with any other install)
uv venv --python 3.12 $B/venv && UV_CACHE_DIR=$B/cache uv pip install --python $B/venv/bin/python harbor
# per run (env -i + empty DOCKER_CONFIG so no outside credentials are read)
OPENROUTER_API_KEY=... $B/hrun.sh run -d terminal-bench@2.0 \
  --agent-import-path bench.harbor_agent:AgentStackAgent \
  -m openrouter:<provider>/<model> -n 1 -l <tasks> -o $B/jobs
```

Results: see the table below (filled as runs complete).

| Model | Tasks run | Passed | Cost (USD) | Notes |
|---|---|---|---|---|
| | | | | |

Known limits of this setup: the task images are amd64 and run emulated on an Apple-silicon Mac,
so agent install is slow and wall-clock times are not representative; a handful of tasks is a tiny
sample; no repeats (Terminal-Bench's own rules ask for 5 attempts per task for a leaderboard
entry, so none of this is leaderboard-eligible).

## What these runs found (and fixed)

- A run longer than ~20 tool calls dropped the user's own request from the model's input
  (message-count window). Fixed: the request is pinned (`ContextBuilder.build(pinned=...)`).
- Old tool results were never cleared; now a token budget clears them first, then drops the oldest
  messages, and summarises old history with the small model.
- The default 50-step limit ended a realistic feature task early; now `AGENT_STACK_MAX_STEPS`.
- Shell commands are not workspace-confined (see Safety rules). Still open.
