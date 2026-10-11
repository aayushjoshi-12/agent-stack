# Design — Skills (`SKILL.md`) and Agent Instructions (`AGENTS.md`)

> Two file-based capability mechanisms every modern harness supports, adapted to
> agent-stack's "one runtime, many gateways" architecture. New phases **P16b** (skills) and
> **P16c** (instructions) in [`../IMPLEMENTATION_PHASES.md`](../IMPLEMENTATION_PHASES.md).

---

## 1. Why both

They solve different problems and must not be merged:

| | `AGENTS.md` | `SKILL.md` |
|---|---|---|
| Nature | **Always-on project context** | **On-demand capability** |
| Loading | Injected into the system context for every turn | Metadata always known; body loaded only when relevant |
| Scope | Per directory tree (nearest wins) | Per named capability |
| Analogy | "How this project works" | "How to do a specific task" |
| Cost model | Fixed tokens every turn (keep small) | ~100 tokens/skill resident; body only when activated |

Both are read by *the runtime running on the user's project*, not just by the coding agent
that builds agent-stack. So agent-stack must ship **loaders + tools**, and it should also
dogfood the formats in its own repo (`AGENTS.md` at the root; `docs/skills/` examples).

---

## 2. `SKILL.md` format (spec summary)

A skill is a directory with a required `SKILL.md`:

```text
my-skill/
├── SKILL.md          # YAML frontmatter + markdown body
├── scripts/          # optional executable helpers
├── references/       # optional on-demand docs
└── assets/           # optional templates/resources
```

**Frontmatter** (YAML):

| Field | Req | Notes |
|---|---|---|
| `name` | ✅ | ≤64 chars; lowercase alnum + single hyphens; must equal the directory name |
| `description` | ✅ | ≤1024 chars; says **what + when to use**; third-person |
| `license` | — | short string |
| `compatibility` | — | ≤500 chars; env requirements |
| `metadata` | — | map of string→string |
| `allowed-tools` | — | space-separated pre-approved tools (experimental) |

**Body**: free Markdown instructions, recommended `< 5000` tokens / `< 500` lines.

**Progressive disclosure (3 levels):**

1. **Metadata** (~100 tokens/skill): `name` + `description` for *all* discovered skills.
2. **Instructions**: full `SKILL.md` body, loaded when the skill is activated.
3. **Resources**: `scripts/`, `references/`, `assets/` files read only on demand.

Reference: <https://agentskills.io/specification>.

---

## 3. `AGENTS.md` format (convention summary)

- Plain Markdown, **no required fields**; a "README for agents".
- One at the repo root; **nested** `AGENTS.md` files in subdirectories for monorepos.
- **Precedence: the file nearest the working directory wins.** Explicit user instructions
  override everything.
- Typical sections: project overview, build/test commands, code style, testing, security.
- Also interoperates with `CLAUDE.md` (same idea); support both, prefer `AGENTS.md`.

Reference: <https://agents.md/>.

---

## 4. Runtime design

### 4.1 New package

```text
src/agent_stack/
├── skills/
│   ├── models.py      # SkillMetadata, Skill, SkillResource
│   ├── loader.py      # discover + parse SKILL.md, validate names
│   └── registry.py    # SkillRegistry: list metadata, load body, load resource
└── context/
    └── instructions.py  # AGENTS.md discovery, nearest-wins resolution
```

Both are **definitions**, not runtime services: they load immutable data used by
`ContextBuilder` and exposed through tools.

### 4.2 Skills

```python
@dataclass(frozen=True)
class SkillMetadata:
    name: str
    description: str
    path: Path            # skill directory
    allowed_tools: tuple[str, ...] = ()
    metadata: Mapping[str, str] = field(default_factory=dict)

@dataclass(frozen=True)
class Skill(SkillMetadata):
    body: str             # the markdown after frontmatter

class SkillRegistry:
    @classmethod
    def discover(cls, roots: Sequence[Path]) -> "SkillRegistry": ...
    def metadata(self) -> list[SkillMetadata]: ...        # level 1, for the prompt
    def load(self, name: str) -> Skill: ...               # level 2
    def read_resource(self, name: str, rel_path: str) -> str: ...  # level 3
```

**Discovery roots** (in order):
1. `<workspace>/.agent/skills/` — project-local, session workspace.
2. `<repo>/skills/` — skills shipped with the project.
3. `~/.claude/skills/` — user-global, **opt-in** via config.

**Progressive disclosure wiring** (the whole point):

- At context-build time, append a compact block to the system prompt:

  ```text
  <available_skills>
    <skill name="pdf-processing">Extract PDF text, fill forms…</skill>
    ...
  </available_skills>
  Use the load_skill tool to read a skill before using it.
  ```

- Expose tools (§14.3 tool naming:

  ```text
  load_skill(name)            -> full SKILL.md body
  read_skill_resource(name, path) -> one resource file (scripts/references/assets)
  ```

- Executing a skill's `scripts/*` goes through the normal **terminal tool** (P14), so it
  inherits policy, timeouts, output caps, and (later) approval. Never a bespoke executor.

**`allowed-tools`**: when a skill declares `allowed-tools`, `QueryRunner`/`ToolContext`
can narrow the tool set available while that skill is active. Experimental — implement as
metadata first, enforcement later.

**Validation** (fail loudly, at discovery):

- `name` matches the directory name and the charset rules;
- `description` non-empty and ≤1024;
- duplicate skill names across roots → the higher-precedence root wins, warn on shadowing.

### 4.3 Agent instructions

```python
class InstructionsLoader:
    def __init__(self, *, filenames=("AGENTS.md", "CLAUDE.md")): ...

    def load(self, target_dir: Path, *, stop: Path) -> list[InstructionFile]:
        """Every instructions file from `target_dir` up to `stop` (root), nearest first."""

@dataclass(frozen=True)
class InstructionFile:
    path: Path
    content: str
    precedence: int   # 0 = nearest
```

**Wiring:** `ContextBuilder.build()` composes the system prompt as:

```text
base system prompt
  + resolved AGENTS.md files (nearest-first, clearly delimited)
  + <available_skills> metadata block
  + memory summary (P21)
  + summary + recent messages (P16)
```

`AGENTS.md` content is **always on**, so it is size-capped (e.g. 8 KB per file, 32 KB
total) and cached by `(path, mtime)`.

### 4.4 What the agent should *not* do

- Do **not** dump every skill body into the prompt. Only metadata is resident.
- Do **not** auto-run skill scripts. The model must load the skill, then call the terminal
  tool (approval applies to dangerous commands).
- Do **not** treat `AGENTS.md` as user input to be executed; it is context only.

---

## 5. Acceptance criteria

**P16b — Skills**
- `test_discovers_skill_metadata` — finds skills, parses frontmatter, ignores dirs without `SKILL.md`.
- `test_rejects_invalid_skill_name` — charset / directory-mismatch rules.
- `test_rejects_missing_description`.
- `test_duplicate_skill_name_shadowed_by_precedence`.
- `test_loader_returns_body_not_metadata` — level 2.
- `test_read_resource_reads_only_within_skill_dir` — path traversal rejected.
- `test_system_prompt_lists_skill_metadata_only` — bodies are absent until loaded (a
  token-budget assertion).

**P16c — Instructions**
- `test_loads_root_agents_md`.
- `test_nested_agents_md_nearest_wins`.
- `test_supports_claude_md_fallback`.
- `test_instruction_size_cap_enforced`.
- `test_instructions_injected_into_system_prompt` (the `ContextBuilder` integration test).

**Smoke (real model):** put a `SKILL.md` in the workspace, ask a question whose answer is
only in the skill body, and confirm the model calls `load_skill` and answers — with the
real LLM, no mocks.

---

## 6. Why this is not scope creep

Skills and `AGENTS.md` are *context sources*, and P16 already establishes the context
boundary. Adding them is an extension of one existing seam (the system-prompt assembly),
not a new architecture. They are gated behind P16 and implemented as definition loaders +
two tools, so they inherit all of the runtime's existing safety machinery.
