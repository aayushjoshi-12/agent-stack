# ADR 009 — Support `SKILL.md` and `AGENTS.md`

**Status:** Accepted

## Context

Modern agent harnesses expose two file-based capability mechanisms: **Agent Skills**
(`SKILL.md`, progressive disclosure) and **agent instruction files** (`AGENTS.md`, nearest
wins). agent-stack should understand both, because it runs against the user's project, not
just its own.

## Decision

Ship two loaders and wire them into context assembly (P16b/P16c):

- **Skills:** discover `SKILL.md` under `<workspace>/.agent/skills/`, repo `skills/`, and
  (opt-in) `~/.claude/skills/`. Level-1 metadata (`name`, `description`) is injected into
  the system prompt; the body loads via a `load_skill` tool; resources via
  `read_skill_resource`. Skill `scripts/` run only through the terminal tool.
- **Instructions:** resolve `AGENTS.md` (fallback `CLAUDE.md`) from the working directory up
  to the root, **nearest file wins**, size-capped and cached, injected as always-on context.

See [`../design/skills-and-instructions.md`](../design/skills-and-instructions.md).

## Consequences

- Skills stay cheap (~100 resident tokens each); bodies load on demand.
- `AGENTS.md` is always-on context and must stay small.
- Both are *definitions* (immutable data), not runtime services.
- Skill scripts inherit tool policy/timeout/approval — no bespoke execution path.
