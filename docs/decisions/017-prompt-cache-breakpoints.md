# ADR 017 — Prompt-cache breakpoints: system message plus a rolling last message, only where needed

**Status:** accepted (2026-11-27).

## Context

Every model call resends the same prefix: tool schemas, the system prompt, the earlier turns.
Providers discount a cached prefix heavily, but they differ in how it is cached. Per OpenRouter's
prompt-caching docs: OpenAI, DeepSeek, Grok, Moonshot, Groq and Z.AI cache automatically; Gemini
2.5+ caches implicitly (explicit `cache_control` is optional, only its last breakpoint counts, and
writes are charged); Anthropic Claude caches only up to blocks marked `cache_control` (at most
four); Alibaba Qwen caches only at explicit marks. `ChatOpenRouter` already maps
`prompt_tokens_details.cached_tokens` / `cache_write_tokens` to `usage_metadata`'s
`input_token_details.cache_read` / `cache_creation`, and passes content blocks through untouched,
so a `cache_control` key on a text block reaches the provider as is (langchain-anthropic does the
same natively).

## Decision

- `models/caching.py` marks two blocks with `cache_control: {"type": "ephemeral"}`: the system
  message (Anthropic orders tools before system, so this caches the tool schemas too) and the last
  message that has text (a rolling breakpoint: the next call's prefix contains this call's write,
  and the provider's lookback finds it). Two of the four allowed.
- Only for `anthropic:*`, `openrouter:anthropic/*` and `openrouter:qwen/*`. Gemini is left to its
  implicit cache: a rolling explicit breakpoint would pay a cache write on every turn for the
  discount implicit caching gives for free. Everything else caches on its own.
- The transform copies the two messages it marks; stored messages are never touched. It is a
  `RunnableLambda` in front of the tool-bound model, applied in `AgentFactory.create` right after
  `bind_tools`, so the loop call and the compaction call both get it and `AgentRuntime` stays
  provider-neutral.
- `model.usage` carries `cache_read_tokens` and `cache_write_tokens`; `SessionUsage` totals both
  and the cost line shows `12k in (6k cached)`.

## Consequences

- Measured through OpenRouter on the real system prompt + tools (~6k tokens): Claude Haiku 4.5's
  second call read 6104 tokens from cache (cost $0.0077 -> $0.00065); DeepSeek V4 Pro read 5376
  automatically ($0.0047 -> $0.0011); Qwen 3.8 Flash wrote then read 5703.
- Anything that changes the prefix (a resident system part changing, the history window sliding,
  a compaction) costs one uncached call; P31 keeps the prefix stable for this reason.
- A new explicit-only provider is one prefix in `_EXPLICIT`.
