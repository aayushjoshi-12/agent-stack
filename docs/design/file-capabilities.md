# File-based capabilities — survey & mapping

> How the agent ecosystem expresses persistent capability through files in the repo,
> and how `agent-stack` maps to each. This is the reference for the context/instructions
> work (P16b/P16c/P16d) and persistent memory (P21).

## The conventions

| File | What it is | Who defines it | Discovery | Semantics |
|---|---|---|---|---|
| `AGENTS.md` | Project instructions for agents | `agents.md` spec (OpenAI/CODEX, adopted by many tools) | Walk directories from the working dir up to the repo root; **every** file found is loaded | Nearest/innermost file wins on conflict; files closer to the searched dir take precedence over shallower ones; `@imports` can pull in other files; size and line caps apply |
| `CLAUDE.md` | Claude Code's project instructions | Anthropic | Same walk-up from cwd to root | Same nearest-wins precedence; `CLAUDE.local.md` (uncommitted, personal) and `.claude/CLAUDE.md` are also honored; `@imports` directive |
| `SKILL.md` | A skill: name + description + body + optional resources/scripts | agentskills.io spec | Known discovery roots (project `.agent/skills/`, repo `skills/`, repo `.claude/skills/`, user `~/.claude/skills/`) | **Progressive disclosure**: level 1 = frontmatter metadata in the system prompt, level 2 = body via `load_skill`, level 3 = resources/scripts on demand |
| `MEMORY.md` | Memory index + topic files | Claude Code auto-memory (also trialed generically as "memory.md") | `MEMORY.md` index at a memory root; topic files alongside it | **Progressive disclosure**: index loaded into context at session start (first ~200 lines / ~25 KB), topic files fetched on demand; the model writes topics itself |
| `.claude/rules/*.md` | Path-scoped rules | Claude Code | `.claude/rules/` matched against the file being edited | Rules apply only when the tool touches matching paths |

## How agent-stack maps (status 2026-10-11)

| Capability | agent-stack phase | Status | Notes |
|---|---|---|---|
| `AGENTS.md` walk-up + nearest-wins | **P16c** (`context/instructions.py`) | ✅ implemented | Also falls back to `CLAUDE.md`; size caps (8 KB/file, 32 KB total), `(path, mtime)` cache |
| `CLAUDE.md` / `CLAUDE.local.md` fallback | **P16c** | ✅ implemented (later) | `AGENTS.local.md` / `CLAUDE.local.md` overlays load ahead of the shared file; `.claude/CLAUDE.md` is still not read |
| `@imports` / per-package rules | **P16c** | 🟡 `@imports` implemented (relative, same directory tree, three levels); per-package permissions deferred | Monorepo per-package permissions — not needed yet |
| `SKILL.md` discovery + progressive disclosure | **P16b** (`skills/`) | ✅ implemented | Roots `workspace/.agent/skills/`, `.agents/skills/`, `.claude/skills/`, repo `skills/`, repo `.claude/skills/` (local only), `AGENT_STACK_SKILLS_PATH` (e.g. `~/.claude/skills`), then the built-in `handoff`/`smoke-test`; the first root wins a name |
| `MEMORY.md` index + topics | **P16d** | ✅ implemented | Progressive disclosure mirrors the skills system; on-disk shape feeds P21 |
| Path-scoped rules (`.claude/rules/`) | — | ⬜ deferred | Would compose with the instructions loader; no forcing requirement yet |
| Automatic memory extraction | **P21** (persistent memory) | ⬜ deferred | P16d gives the *file* format; P21 gives the *store + tools* |

## P16d — Memory files (`MEMORY.md`) [implemented]

**Goal.** Support the file-based memory convention: a `MEMORY.md` index + topic files,
with the same three-level progressive disclosure as skills.

**Discovery roots** (same shape as skills): `<workspace>/.agent/memory/`,
repo `memory/` (optional), `~/.agent/memory/` (opt-in). Fallback: a bare `MEMORY.md`
at the workspace root.

**Shape**
```text
<memory root>/
├── MEMORY.md            # index: one short line per topic ("TopicName — one-line summary")
└── topics/
    └── <topic-name>.md  # full topic content (path-scoped, no traversal)
```

**Injection**
- Level 1: `MEMORY.md` index appended to the system prompt (capped, delimited), same as
  skills metadata and instructions.
- Level 2: tool `read_memory_topic(name)` — returns the topic body.
- Level 3: tool `write_memory_topic(name, content)` — creates/updates a topic and its
  index line (mtime frontmatter, like the convention).

**Acceptance**
- `test_memory_index_loaded_capped` — index lines, not bodies, reach the system prompt.
- `test_read_topic_on_demand_scoped_to_memory_dir` — path traversal rejected.
- `test_write_topic_updates_index_entry`.

**Defer:** automatic extraction (P21), topic GC/merge, versioned memory.