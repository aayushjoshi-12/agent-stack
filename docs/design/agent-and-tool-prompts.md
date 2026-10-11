# Agent & tool prompts (Claude Code-inspired)

Source of inspiration: the latest Claude Code system prompt from
[cchistory.mariozechner.at](https://cchistory.mariozechner.at/) — version **2.1.112**
(released 2026-04-16), fetched 2026-10-11. How to refresh this reference later:
see [Refreshing this note](#refreshing-this-note) at the end.

This note distills what we borrow for agent-stack's own prompts: the default agent
system prompt, the builtin tool descriptions, and the subagent delegation guidance
(P20). Everything here is the actual copy that lands in `src/`; nothing speculative.

## What we borrow, at a glance

| Claude Code pattern | Where it lands here |
| --- | --- |
| Dedicated tools over shell, each with "prefer this instead of Bash" guidance | `tools/builtins/` descriptions |
| Exact-string edit semantics + uniqueness rule | `edit_file` description |
| "State what the command does" + avoid risky-words | N/A — our `run_command` result is plain; intent is set by the agent's message |
| Independent tool calls batched in one message; dependent chained with `&&` | default agent system prompt |
| Parallel + background runs, no polling/sleeping | system prompt + P14 streaming events |
| Subagent prompt writing (self-contained brief, answer form, length cap) | P20 `delegate` tool description + this doc |
| "Skill matches → invoke before responding; never mention without invoking" | default agent system prompt (P16b skills) |
| Auto memory = `MEMORY.md` index + topic files | already our P16d spec (`file-capabilities.md`) |
| Comment discipline, no planning docs unless asked | default agent system prompt |
| Git safety (no amend, no destructive ops, stage by name, never commit unless asked) | repo `AGENTS.md` already encodes this |

## What we deliberately do NOT copy

- **`Co-Authored-By` commit trailers** — repo rule forbids AI trailers.
- **ToolSearch / deferred-tool schemas** — we bind a fixed toolset per agent today;
  progressive tool loading is a later design question, not assumed.
- **"Never write docs unless asked"** — this repo *is* documentation-heavy by design;
  we keep only "don't create planning/analysis docs unless asked".
- **/loop, ScheduleWakeup, worktree isolation** — out of scope for a personal runtime
  v1; the input-injection mechanism (P20) is our equivalent of interrupt-and-respond.

---

## 1. Default agent system prompt

Replaces `DEFAULT_SYSTEM_PROMPT` in `app/bootstrap.py`. Written for our toolset and
for the subagent/parent relationship we are building (P20), but keeps the runtime's
promise of "answer directly and concisely".

```text
You are a helpful personal assistant. Answer the user's request directly and
concisely — no preamble, no recap of what you did unless asked.

Tools:
- Prefer the dedicated tools (list_directory, read_file, write_file, edit_file,
  search_files) over run_command for the same job: they are safer, scoped to the
  workspace, and easier to review.
- When you make several independent tool calls, put them in a single message so
  they run in parallel. Chain dependent commands with `&&` inside run_command.
- Read only the parts of files you need. Use search_files to find files instead
  of guessing paths.
- edit_file replaces the single unique occurrence of `old` with `new`. If the
  text appears more than once, widen `old` with surrounding context; if it
  appears zero times, re-read the file and retry exactly.
- If a skill matches the task, invoke it before generating any other response.
  Never mention a skill without invoking it, and never guess a skill name.

Code style:
- Follow the repo's AGENTS.md. Don't create planning or analysis documents
  unless the user asks.
- Default to no comments in code — one short line max when one is needed.
```

The following paragraph is **appended when P20 (subagents) lands** — until a
`delegate` tool exists, instructions about it would be inert, so they are held back:

```text
Subagents (when available):
- Delegate broad exploration or heavy research to a subagent to protect your
  context window. Do not duplicate work a subagent is already doing.
- Write delegation prompts that are self-contained: the goal, what to check,
  and the form and length of the answer (e.g. "Report a punch list, under 200
  words").
```

## 2. Builtin tool descriptions

Each description follows the Claude Code shape: what it does, when to prefer it
over the shell, and the sharp edge (cap/error) the model must know. These replace
the current docstrings in `tools/builtins/filesystem.py` and `terminal.py`.

### `list_directory`

```text
List the entries of a directory inside the workspace (files show their size,
directories a trailing slash). Use this instead of `ls` — it is scoped to the
workspace and easy to review.
```

### `read_file`

```text
Read a UTF-8 text file as a string. Use this instead of `cat`/`head`/`tail`.
Fails if the file does not exist or is larger than the 1 MiB read cap — for big
files, search first or read the relevant region via other tools. Inside the
workspace only.
```

### `write_file`

```text
Create or overwrite a file with the given content. Use this instead of
`echo >` / `cat <<EOF`. Missing parent directories are created. Content is
capped at 1 MiB. To change part of an existing file, prefer edit_file.
Inside the workspace only.
```

### `edit_file`

```text
Replace the single unique occurrence of `old` with `new` in a file. Exact
string match, preserving indentation exactly. The edit is rejected if `old`
appears zero times (re-read and retry) or more than once (widen `old` with
surrounding context until it is unique). Prefer this over rewriting a whole
file: it sends only the diff. Inside the workspace only.
```

### `search_files`

```text
Find files by glob name pattern under a directory, e.g. `pattern="*.py"`.
Returns up to 200 matches, resolved relative to the workspace root. Use this
instead of `find`. It matches file names, not contents.
```

### `run_command`

```text
Run a command in the workspace and capture stdout/stderr. No implicit shell:
the command is executed directly unless shell=True is passed explicitly.
Prefer the dedicated tools for filesystem work — read_file (not cat/head/tail),
edit_file (not sed/awk), write_file (not echo), search_files/list_directory
(not find/ls); they are scoped, capped, and easier to review.
The working directory resets each call to the workspace root unless cwd is
given. Output is capped. A non-zero exit code is reported in the result, not
raised. The whole process group is killed if the command exceeds timeout
(default 120s), reported as timed out.
```

## 3. Subagent delegation (P20) — prompt guidance + description

The child agent starts with **no context from this conversation** (like Claude
Code's Agent tool). The parent's `delegate`/`subagent_feed` prompts must therefore
be self-contained. Guidance we put in the `delegate` tool description and in the
P20 design:

1. **State the goal** in one sentence.
2. **List what to check / the steps** — enough that the child never has to ask.
3. **Specify the answer form + length cap** — "Report a punch list, under 200
   words", "Return: is this safe, and if not, what specifically breaks?".
4. **Brief the context it can't see** — what the parent already knows/verified,
   so the child doesn't re-derive it.
5. **Don't duplicate afterwards** — once delegated, the parent doesn't redo the
   child's searches.

Draft description for the `delegate` tool (P20, `subagents/delegation.py`):

```text
Delegate a task to a named subagent. The child starts with no context from this
conversation, so the task must be self-contained: the goal, what to check, and
the form and length of the answer. Use for broad exploration or heavy research
that would otherwise flood your context — the child's full transcript stays out
of your window; you receive a compact report. Do not delegate trivial lookups,
and do not repeat the child's work yourself afterwards. The child runs
concurrently and can be steered with subagent_status / subagent_interrupt /
subagent_feed.
```

Strategy rule (mirrors Claude Code): use a subagent for exploration that would
take more than a few search rounds; otherwise use search_files/run_command
directly.

## 4. Relationship to existing design docs

- `file-capabilities.md` — P16d memory files (`MEMORY.md` index + topic files)
  match Claude Code's auto memory; the system prompt does not need to describe
  memory mechanics, the context builder injects them.
- `skills-and-instructions.md` — P16b skills + P16c instructions; the system
  prompt's "invoke a matching skill before responding" line ties into the
  `skill_tools` the registry already exposes.

## 5. Deployment plan

1. Apply §1 to `app/bootstrap.py` (`DEFAULT_SYSTEM_PROMPT`).
2. Apply §2 to `tools/builtins/filesystem.py` + `terminal.py` docstrings.
3. §3 lands with the P20 `delegate` tool implementation.

None of §2 changes behavior — descriptions only. §1 is behavior (the model's
instructions); verify with a smoke run before committing.

---

## Refreshing this note (site reference)

**Site:** [cchistory.mariozechner.at](https://cchistory.mariozechner.at/) — *cchistory,
Claude Code Version History*, by Mario Zechner. Tracks Claude Code's system prompt
across versions (currently 1.0.0 → latest), with a side-by-side diff viewer.

The site is a static SPA; the prompt text is served as raw markdown via two stable
data endpoints (no API key, public):

| Endpoint | Contents |
| --- | --- |
| `/data/versions.json` | `{"versions": [{"version": "1.0.0"}, ...], "lastUpdated": "<ISO>"}` — version list + freshness timestamp |
| `/data/prompts-<version>.md` | The full system prompt text for one version, e.g. `/data/prompts-2.1.112.md` |

**Refresh procedure** (mechanical, ~1 minute):

```bash
# 1. Find the newest version + lastUpdated
curl -s https://cchistory.mariozechner.at/data/versions.json | jq '{lastUpdated, newest: .versions[-1]}'

# 2. Fetch the latest prompt
curl -sL "https://cchistory.mariozechner.at/data/prompts-<newest>.md" -o /tmp/cc-latest.md

# 3. Compare against the version this note is based on (2.1.112, 2026-04-16):
#    * meaningful changes to tool descriptions / subagent guidance / memory
#      conventions → update §1–§3 copy and this header's version + dates
#    * changes irrelevant to agent-stack (e.g. /loop, pricing, sandboxing) → note
#      the new version in this header, no copy change
```

Notes: `lastUpdated` on the site was `2026-10-11T09:30:05.515Z` when we fetched the
2.1.112 prompt; prompt files are the *system prompt only* (the file starts with a
`# Claude Code Version <v>` heading). A local copy of the 2.1.112 prompt lives in
`tmp/` (gitignored) as reference material, not part of the repo.