# Idea (not started): make agent-stack work well with local models on Ollama

Status: **future idea, nothing built.** Parked on 2026-11-14 until the remaining agent-stack
work (see `STATUS.md`) is finished. Everything under "Hypotheses" is unverified.

## Goal

Run agent-stack on a 16 GB Apple silicon Mac (M4) with small open-weight models served by
Ollama, at zero cost, and still get useful agent behaviour. The research question: which
combination of model, quantization and runtime changes gives the best task success per GB
and per second for *agent* workloads (long repeated prefix, valid tool calls, growing
context), not chat throughput.

## What exists today

- `models/factory.py` builds any model from a `provider:model` string via LangChain's
  `init_chat_model`, so `ollama:<model>` should work once `langchain-ollama` is a dependency.
  Today only `langchain` and `langchain-openrouter` are installed.
- Defaults assume a large cloud model: `openrouter:deepseek/deepseek-v4-pro`, a 1M-token
  `AGENT_STACK_CONTEXT_WINDOW`, a 20 KB artifact threshold.
- Ollama (0.40.x) is installed on the dev machine with only an embedding model pulled.

## Hypotheses to test (each needs a measurement before it counts)

1. Ollama defaults to a small context unless `num_ctx` is set, so the runtime must set it
   explicitly and keep `AGENT_STACK_CONTEXT_WINDOW` consistent with it.
2. KV-cache reuse needs a byte-identical prompt prefix. Volatile content near the top of the
   prompt (timestamps, recalled memory, reordered tool schemas) would defeat it.
3. Small models fail on tool calls. Fewer and simpler tool schemas, shorter descriptions,
   a repair-and-retry step, or Ollama structured output may help.
4. The 20 KB artifact threshold and tool-output cap should scale with the model's window.
5. Local runs are slow, so deadlines and tool timeouts need local-friendly defaults.
6. Context compaction probably matters more locally than the choice of model.
7. Other levers worth a look: KV-cache quantization, speculative decoding, and a
   local-first cascade that escalates to an API model only on failure.

## Plan, when picked up

1. Baseline: add `langchain-ollama`, pull one ~4B and one ~8B tool-capable model (check
   Ollama's current library; shortlist and get approval first), run real tasks, record what
   breaks.
2. Measurement harness: a fixed set of 30 to 50 tasks (plus a public function-calling
   subset such as BFCL for comparability) that runs under any `AGENT_STACK_MODEL` and reports
   success, time per task, peak memory, tokens.
3. Optimize one lever at a time. A change counts only if reverting it makes the harness
   result measurably worse.
4. Write up the method and numbers (first blog post). Claim results for a 16 GB M4 only.

## Constraints

- Zero cost: open weights, local inference; free-tier APIs only for the cascade experiment.
- Disk: models are 2 to 5 GB each; keep two or three at a time.
- Work on a branch (e.g. `local-models`), not `main`.
