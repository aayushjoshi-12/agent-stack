# agent-stack documentation

| Doc | Purpose |
|---|---|
| [`STATUS.md`](STATUS.md) | **Start here.** Where the project stands, known limits, decisions already made. |
| [`IMPLEMENTATION_PHASES.md`](IMPLEMENTATION_PHASES.md) | The phased build plan (P00–P32) with acceptance criteria; later notes mark what changed. |
| [`diagrams/`](diagrams/) | Architecture diagrams (Mermaid source `.mmd` + rendered `.svg`). |
| [`WORKFLOW.md`](WORKFLOW.md) | How work is planned, executed, verified (smoke tests), and committed. |
| [`TEST_PLAN.md`](TEST_PLAN.md) | Test layers, the deterministic fake model, and what is covered. |
| [`BENCHMARKS.md`](BENCHMARKS.md) | Small benchmark runs on cheap models, and the safety rules for unattended runs. |
| [`RELEASE.md`](RELEASE.md) | Checklist before the repository goes public. |
| [`decisions/`](decisions/) | Architecture Decision Records (ADRs), with superseded ones marked. |
| [`design/conversation-storage.md`](design/conversation-storage.md) | File-based transcripts/artifacts; SQLite as index. |
| [`design/event-log-and-branching.md`](design/event-log-and-branching.md) | Durable event log + conversation branching (Milestone H). |
| [`design/skills-and-instructions.md`](design/skills-and-instructions.md) | `SKILL.md` + `AGENTS.md` support. |
| [`design/file-capabilities.md`](design/file-capabilities.md) | AGENTS.md, CLAUDE.md, SKILL.md, MEMORY.md conventions and how they map. |
| [`design/agent-and-tool-prompts.md`](design/agent-and-tool-prompts.md) | The prompts, borrowed from Claude Code's. |
| [`design/tui.md`](design/tui.md) | TUI layout, themes and conventions. |
| [`design/source-design-document.md`](design/source-design-document.md) | Original architecture design document (snapshot, not kept current). |
| [`ideas/`](ideas/) | Parked ideas, nothing built. |

## Suggested workflow

1. Pick the next piece of work (`STATUS.md` lists what is open); give it the next phase number.
2. Implement it with its acceptance tests.
3. Smoke-test what touches a real service/DB/filesystem/LLM; keep `make check` green.
4. Commit with the phase key.
5. Record any non-obvious choice as an ADR in `decisions/`.

## Gateway stack (user decisions)

- **TUI first** (Textual + Rich), then **Telegram**, then **Web** (Next.js + SSE — no
  WebSocket).
- Web design is driven by the **design skills** in `.claude/skills/` (local only, gitignored);
  the owner shared the inspiration and the style is ADR 013. All three gateways are built.
