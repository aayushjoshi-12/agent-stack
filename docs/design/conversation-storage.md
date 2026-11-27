# Design — File-based conversation & artifact storage

> Whole chats are stored **as files per session**, not as rows in SQLite. SQLite is the
> index. New behavior for **P09** in [`../IMPLEMENTATION_PHASES.md`](../IMPLEMENTATION_PHASES.md).

---

## 1. Problem

Storing the entire conversation in SQLite means:

- giant opaque `message_json` blobs that are painful to read, diff, grep, or review;
- artifacts (files generated or shared in the chat) living outside the conversation, or as
  base64 inside a column;
- a database that grows with context rather than with *events*;
- no easy way for a human to open "that conversation" and see the whole thing.

## 2. Decision

Two stores, one job each:

```text
ConversationStore  (files)   → the full, human-reviewable conversation + artifacts
SessionStore / RunStore      → SQLite metadata + run lifecycle + message *index*
```

Nothing about this changes `AgentRuntime`: it still receives a `SessionStore`-shaped
dependency. The composition root decides which implementation it gets.

## 3. Layout

```text
<workspace>/.agent/sessions/<session_id>/
├── session.json      # session metadata (mirror of the SQLite row)
├── transcript.md     # human-readable, append-only
├── transcript.jsonl  # machine-readable, append-only (one record per line)
├── artifacts/        # files generated or shared in this chat
│   └── <artifact_id>__<name>
├── subagents/        # P46: each subagent's conversation, same layout + subagent.json
│   └── <agent>-<id>/ #   (run ids, task, status, counts, error)
└── (later) run-<run_id>.md   # optional per-run view
```

Artifacts shared **into** the chat (e.g. a file the user uploaded) and generated **by** the
chat (e.g. a diff, a log) both land in `artifacts/` and are referenced by id everywhere.

## 4. `transcript.jsonl` record shapes

One JSON object per line; append-only; never rewritten. This is the source of truth.

```jsonl
{"seq":1,"ts":"2026-10-11T12:00:00Z","run_id":"r1","kind":"human","content":"run the tests"}
{"seq":2,"ts":"...","run_id":"r1","kind":"ai","content":"","tool_calls":[{"id":"call-1","name":"terminal","args":{"command":"pytest"}}]}
{"seq":3,"ts":"...","run_id":"r1","kind":"tool","tool_call_id":"call-1","content":"12 passed"}
{"seq":4,"ts":"...","run_id":"r1","kind":"ai","content":"All 12 tests passed."}
{"seq":5,"ts":"...","run_id":"r1","kind":"event","type":"tool.started","data":{"tool":"terminal"}}
```

`transcript.md` is a rendered view of the same stream (human-facing; may be regenerated).

**Images (P44, ADR 019).** A user message or tool result with images keeps only its text in
`content`; each image is written once to `artifacts/<sha256[:16]>.<ext>` (content addressed:
the same image sent twice is one file) and named in `data.images`:

```jsonl
{"seq":6,"ts":"...","run_id":"","kind":"human","content":"what is this?","data":{"images":["64b72fba0d73921b.png"]}}
```

Reading the conversation turns each name back into an image block (images first, then the
text), so later turns and a restarted process show the model the same image. A missing file
becomes a `[image: <name> (missing)]` text part. `transcript.md`, the export (P41), the chat
search index (P32) and history rows show `[image: <name>]`; base64 never lands in a text file,
the event log or the audit log (no event carries message content). A branch copies the image
files its lines name.

## 5. Interfaces

```python
@dataclass(frozen=True)
class MessageRecord:
    seq: int
    ts: datetime
    run_id: str
    kind: Literal["human", "ai", "tool", "system", "event"]
    content: str = ""
    tool_calls: tuple[dict, ...] = ()
    tool_call_id: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)

class ConversationStore(Protocol):
    async def append(self, session_id: str, records: Sequence[MessageRecord]) -> None: ...
    async def read(self, session_id: str) -> list[MessageRecord]: ...
    async def read_messages(self, session_id: str) -> list[BaseMessage]: ...  # for the model
    def session_dir(self, session_id: str) -> Path: ...

class ArtifactStore(Protocol):   # scoped to a session in the file backend
    async def put(self, *, session_id: str, data: bytes, name: str) -> ArtifactRef: ...
    async def get(self, ref: ArtifactRef) -> bytes: ...
```

`read_messages()` performs the JSONL → LangChain message conversion at the **model
boundary** (ADR 001/003): the domain never sees raw rows.

## 6. SQLite index schema (no message payloads)

```text
sessions(id, owner_id, agent_name, title, status, version, created_at, updated_at, metadata_json)

runs(id, session_id, parent_run_id, status, step_count, tool_call_count,
     started_at, finished_at, error, metadata_json)

message_index(id, session_id, run_id, seq, kind, jsonl_offset, created_at)
     -- index into transcript.jsonl; content stays in the file

artifacts_index(id, session_id, run_id, name, rel_path, mime_type, size_bytes, created_at)
```

`message_index` is optional in the first cut (you can rebuild it from the JSONL); it exists
so cross-session queries ("find sessions that used the terminal tool") don't have to read
every file.

**As built (2026-11-26).** `message_index` and `artifacts_index` exist in the schema and
`SqliteIndex` can write and read them (tested), but nothing in the running app writes them: no
query has needed offsets. Cross-session search instead uses `chat_fts` (P32, ADR 016), an FTS5
table that does hold a **copy** of message text. It is a disposable search index rebuilt from the
JSONL on demand, so the transcript files stay the only source of truth; this amends "no message
payloads" above for that one table.

## 7. Consequences

- Conversations are reviewable: open the session directory and read `transcript.md`.
- Backup/export is `cp -r` of a session directory (plus the small SQLite file).
- Deleting a session = delete the directory + its index rows (no orphans).
- Concurrency: append-only JSONL + the per-session lock (P12). One writer per session.
- Recovery replays `transcript.jsonl`; there is exactly one durable representation.
- Trade-off: cross-session analytical queries need the index or a scan. Acceptable for a
  personal agent; revisit only if that becomes a real requirement.

## 8. Acceptance

- `test_transcript_jsonl_roundtrips_messages` (write → read → identical messages)
- `test_transcript_md_contains_full_chat`
- `test_session_dir_deleted_with_session`
- `test_sqlite_has_no_message_payload_columns`
- Integration: send messages, restart the process, `read()` returns the full history.

## 9. Lifecycle: what deletion and GC remove (P50)

Deletion cannot be undone, so the rules keep anything a conversation, a branch or a running
process can still reach.

**Deleting a chat** (`Application.delete_session`, every gateway's delete) removes its folder
(transcripts, images, nested subagent folders), its rows in every session-keyed table
(`sessions`, `runs`, `checkpoints`, `todos`, `event_log`, `chat_fts`, ...), its file changes and
their snapshots, and its subagents' sessions: every session whose id starts with `<chat id>::`
or, before P46, `<one of its run ids>::` (rows, and the old flat folder). It does not remove
branches forked from it: each branch is its own conversation with its own copy of the
transcript. Workspace artifacts are left to the next GC.

**`agent-stack gc`** reports what is left behind; `agent-stack gc --apply` deletes it. It never
runs by itself. Its rules:

1. A session with a `sessions` row is kept, unless it is a subagent session whose chat (the id's
   first `::` segment) is neither a session nor a run of a kept session: nothing can open it.
2. Rows of a session with no `sessions` row are deleted, with its snapshots and its folder (the
   rows show this index owned it).
3. A folder the index has no trace of is reported and kept: with `AGENT_STACK_DB` it may belong
   to another index. Delete it by hand if it does not.
4. A workspace artifact (`.agent/artifacts/`) is deleted when no `transcript.jsonl` under
   `sessions/` (any folder, indexed or not; branches and subagents included) and no `event_log`
   row of a kept session mentions its id. Leftover `.tmp-*` files count as unreferenced.
5. A change snapshot folder (`.agent/changes/`) is deleted when no session and no
   `file_changes` row maps to it.
6. Anything touched in the last 24 hours (a file modified, an event logged) is kept: a run in
   another process may have stored an artifact before its messages are saved at the run's end.

**Event-log retention.** Events live exactly as long as their conversation. Replay, fork and
rewind points, history and cost totals read old events, so a kept chat keeps its whole
timeline; deleting the chat and rule 2 are the bound. There is no time- or count-based pruning.

**Approvals.** The broker forgets a request as soon as the waiting run has its answer (approved,
rejected or cancelled); deciding it again is refused as unknown.
