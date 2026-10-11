# agent-stack documentation

| Doc | Purpose |
|---|---|
| [`IMPLEMENTATION_PHASES.md`](IMPLEMENTATION_PHASES.md) | **Start here.** The ordered, phased build plan with acceptance criteria. |
| [`WORKFLOW.md`](WORKFLOW.md) | How work is planned, executed, verified (smoke tests), and committed. |
| [`TEST_PLAN.md`](TEST_PLAN.md) | Test layers, the deterministic fake model, and coverage per phase. |
| [`design/conversation-storage.md`](design/conversation-storage.md) | File-based transcripts/artifacts; SQLite as index. |
| [`design/skills-and-instructions.md`](design/skills-and-instructions.md) | `SKILL.md` + `AGENTS.md` support. |
| [`design/source-design-document.md`](design/source-design-document.md) | Original architecture design document (snapshot). |
| [`decisions/`](decisions/) | Architecture Decision Records (ADRs). |

## Suggested workflow

1. Pick the next phase in `IMPLEMENTATION_PHASES.md` (respect the dependency matrix).
2. Implement it with its acceptance tests.
3. Smoke-test what touches a real service/DB/filesystem/LLM; keep `make check` green.
4. Commit with the phase key.
5. Record any non-obvious choice as an ADR in `decisions/`.
