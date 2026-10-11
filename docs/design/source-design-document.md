# Personal Agent — Architecture and Implementation Design Document

**Status:** Design proposal  
**Audience:** One developer building a personal, extensible agent runtime from first principles  
**Primary language:** Python 3.12+  
**Package manager:** `uv`  
**Primary LangChain primitives:** `init_chat_model`, `BaseChatModel`, LangChain messages, `BaseTool`  
**Suggested runtime style:** async-first, single process initially  
**Persistence:** SQLite first, PostgreSQL later  
**Gateways:** Web, Telegram, TUI  
**Primary capabilities:** coding, terminal access, file operations, long-horizon tasks, subagents, parallel tool calls, streaming, persistent sessions  
**Design goal:** Learn the architecture by writing the internals yourself; do not reproduce DeepAgents 1:1.

---

# TL;DR

Build the system as a **small agent runtime surrounded by adapters**, not as a collection of gateway-specific agents.

The most important abstractions should be:

```text
AgentDefinition
    └── describes WHAT an agent is

AgentRuntime
    └── executes an AgentDefinition

Session
    └── durable conversational/work context

Run
    └── one execution of an agent inside a session

ToolRegistry
    └── maps model tool calls to BaseTool implementations

ToolExecutor
    └── executes independent tool calls concurrently and safely

MemoryStore
    └── cross-session persistent facts

CheckpointStore
    └── durable run/session state for resume

ArtifactStore
    └── large files and outputs outside model context

EventBus
    └── streams safe execution events to gateways

Gateway
    └── Telegram / Web / TUI adapter

SubAgentRegistry
    └── definitions for specialized child agents
```

The central execution loop should initially be explicit and boring:

```text
user input
    ↓
load session
    ↓
append HumanMessage
    ↓
model.ainvoke(...)
    ↓
AIMessage
    ├── no tool calls ──────→ final response
    │
    └── tool calls
            ↓
      execute concurrently
            ↓
      ToolMessages
            ↓
      checkpoint state
            ↓
      model again
            ↓
          repeat
```

Do **not** begin by implementing planning middleware, skill loaders, vector memory, distributed workers, a graph compiler, or a generalized middleware framework.

Implement those only when the base runtime demonstrates that you need them.

---

# 1. What You Are Actually Building

The project is best thought of as a **personal agent operating system**, rather than simply an LLM wrapper.

You want one underlying agent to be reachable from multiple interfaces:

```text
                       ┌────────────────────┐
                       │    Telegram        │
                       └─────────┬──────────┘
                                 │
                       ┌─────────▼──────────┐
                       │       Web          │
                       └─────────┬──────────┘
                                 │
                       ┌─────────▼──────────┐
                       │       TUI          │
                       └─────────┬──────────┘
                                 │
                         Gateway Protocol
                                 │
                    ┌────────────▼────────────┐
                    │      Application        │
                    │       Service Layer     │
                    └────────────┬────────────┘
                                 │
                    ┌────────────▼────────────┐
                    │      Agent Runtime      │
                    │                         │
                    │ model → tools → model  │
                    │          ↕              │
                    │       subagents         │
                    └────────────┬────────────┘
                                 │
            ┌────────────────────┼────────────────────┐
            │                    │                    │
     CheckpointStore       MemoryStore          ArtifactStore
            │                    │                    │
        SQLite/PG            SQLite/PG         Local FS/S3 later
```

The central runtime must not know whether a request came from Telegram, a browser, or a terminal.

Likewise, the Telegram gateway should not know how the agent executes tools.

This separation is the single most important architectural decision in this project.

---

# 2. Design Goals

## 2.1 Primary goals

The runtime should provide:

1. Provider-independent chat model initialization.
2. Tool calling through standardized LangChain interfaces.
3. Multiple tool calls executed concurrently when safe.
4. Persistent conversations.
5. Persistent execution state.
6. Resumable long-running tasks.
7. Multiple gateways over the same agent/session system.
8. Subagents.
9. Streaming execution events.
10. Cancellation.
11. Tool failure recovery.
12. Bounded execution.
13. Context management for long conversations.
14. Safe terminal/file access.
15. A codebase small enough that you understand every abstraction.

## 2.2 Secondary goals

Later:

- human approval for dangerous operations
- memory retrieval
- task planning / TODOs
- scheduled tasks
- background jobs
- model routing
- MCP tools
- skills
- semantic memory
- web research
- remote sandboxes
- distributed execution

## 2.3 Explicit non-goals for version 1

Do not build:

- a general-purpose agent framework
- a LangChain replacement
- a generalized middleware system
- a distributed task queue
- Kubernetes deployment
- multiple databases
- vector search
- automatic memory extraction
- browser automation
- generalized workflow DAG execution
- model fine-tuning
- a plugin marketplace
- a 1:1 DeepAgents recreation

The first version should be a **good personal agent runtime**, not a framework for everyone.

---

# 3. Important Current LangChain / DeepAgents Context

As of this design's verification date, LangChain exposes `init_chat_model` as a unified way to initialize supported chat models. It accepts provider-qualified model identifiers and returns a common chat model interface. It can also create configurable models, although runtime configuration should be restricted to explicitly allowed fields when inputs are untrusted.

Useful references:

- LangChain `init_chat_model`: https://reference.langchain.com/python/langchain/chat_models/base/init_chat_model
- LangChain tool reference: https://reference.langchain.com/python/langchain-core/tools
- LangChain `ToolMessage`: https://reference.langchain.com/python/langchain-core/messages/tool/ToolMessage
- LangChain tool calling: https://www.langchain.com/blog/tool-calling-with-langchain
- LangGraph persistence: https://github.com/langchain-ai/docs/blob/main/src/oss/langgraph/persistence.mdx
- LangGraph `StateGraph`: https://reference.langchain.com/python/langgraph/graph/state/StateGraph
- DeepAgents: https://github.com/langchain-ai/deepagents
- DeepAgents graph assembly: https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/graph.py
- Deep agent from scratch: https://github.com/langchain-ai/docs/blob/main/src/oss/langchain/deep-agent-from-scratch.mdx

The relevant standardized tool-calling flow is essentially:

```python
model = init_chat_model("openai:gpt-5.5", temperature=0)
model_with_tools = model.bind_tools(tools)
response = await model_with_tools.ainvoke(messages)

for call in response.tool_calls:
    ...
```

That standardization is exactly what you want to leverage. Your runtime should not care whether the call came from OpenAI, Anthropic, Gemini, Groq, Mistral, etc., provided the selected model implements the capabilities you require.

DeepAgents is currently positioned as an opinionated harness above LangChain's agent abstraction and LangGraph runtime, assembling features such as planning, filesystem access, subagents, memory, summarization, and middleware. It is useful as an architectural reference, but copying its entire machinery would defeat your stated learning goal.

---

# 4. Core Architectural Principle

There are four concepts that must not be conflated:

## Agent

An agent describes:

> What model, tools, instructions, and subagents are available?

It is mostly configuration.

## Session

A session describes:

> What persistent conversation/work context am I operating in?

A session survives individual requests.

## Run

A run describes:

> What execution is happening right now because of one user request?

A single session may contain many runs.

## Gateway

A gateway describes:

> How does a human or external system communicate with the agent?

Telegram, web, and TUI are all gateways.

This gives you:

```text
User
  │
  │ message
  ▼
Gateway
  │
  │ MessageEnvelope
  ▼
SessionManager
  │
  │ session_id
  ▼
AgentRuntime
  │
  ├── Run
  │    ├── model calls
  │    ├── tool calls
  │    ├── subagents
  │    └── checkpoints
  │
  ├── MemoryStore
  ├── ArtifactStore
  └── EventBus
```

---

# 5. Recommended Package Structure

Start with this structure:

```text
personal-agent/
├── pyproject.toml
├── README.md
├── uv.lock
├── .env.example
├── .gitignore
│
├── src/
│   └── personal_agent/
│       ├── __init__.py
│       │
│       ├── core/
│       │   ├── agent.py
│       │   ├── runtime.py
│       │   ├── state.py
│       │   ├── session.py
│       │   ├── run.py
│       │   ├── events.py
│       │   ├── context.py
│       │   ├── limits.py
│       │   └── errors.py
│       │
│       ├── models/
│       │   ├── factory.py
│       │   └── configuration.py
│       │
│       ├── tools/
│       │   ├── registry.py
│       │   ├── executor.py
│       │   ├── policies.py
│       │   └── builtins/
│       │       ├── terminal.py
│       │       ├── filesystem.py
│       │       ├── search.py
│       │       └── memory.py
│       │
│       ├── subagents/
│       │   ├── registry.py
│       │   ├── delegation.py
│       │   └── supervisor.py
│       │
│       ├── persistence/
│       │   ├── base.py
│       │   ├── sqlite.py
│       │   ├── migrations.py
│       │   └── serializers.py
│       │
│       ├── memory/
│       │   ├── store.py
│       │   └── models.py
│       │
│       ├── artifacts/
│       │   ├── store.py
│       │   └── local.py
│       │
│       ├── gateways/
│       │   ├── base.py
│       │   ├── web/
│       │   │   ├── app.py
│       │   │   ├── routes.py
│       │   │   └── stream.py
│       │   ├── telegram/
│       │   │   ├── adapter.py
│       │   │   └── bot.py
│       │   └── tui/
│       │       ├── app.py
│       │       └── adapter.py
│       │
│       ├── observability/
│       │   ├── logging.py
│       │   ├── metrics.py
│       │   └── tracing.py
│       │
│       └── app/
│           ├── bootstrap.py
│           ├── config.py
│           └── dependencies.py
│
├── tests/
│   ├── unit/
│   ├── integration/
│   └── e2e/
│
├── migrations/
├── workspace/
└── scripts/
```

Do not create all of these files on day one. Create each package only when you reach its implementation phase.

---

# 6. Core Object Model

A useful first mental model is:

```text
AgentDefinition
      │
      ▼
AgentRuntime
      │
      ├────────── Session
      │               │
      │               ├── Message history
      │               ├── working state
      │               └── metadata
      │
      ├────────── Run
      │               │
      │               ├── model turns
      │               ├── tool executions
      │               ├── checkpoints
      │               └── events
      │
      ├────────── ToolRegistry
      │
      ├────────── SubAgentRegistry
      │
      ├────────── MemoryStore
      │
      └────────── ArtifactStore
```

---

# 7. Agent Definition

Do not put execution state directly on `Agent`.

The `Agent` should be reusable and effectively immutable.

A reasonable first version:

```python
from dataclasses import dataclass
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool


@dataclass(frozen=True)
class Agent:
    name: str
    model: str | BaseChatModel
    tools: tuple[BaseTool, ...] = ()
    subagents: tuple["SubAgentSpec", ...] = ()
    system_prompt: str = ""
    limits: "AgentLimits" = AgentLimits()
```

Your public constructor should eventually support:

```python
agent = Agent(
    name="personal",
    model="openai:gpt-5.5",
    tools=[terminal_tool, read_file_tool, write_file_tool],
    subagents=[researcher, coder],
    system_prompt=SYSTEM_PROMPT,
    limits=AgentLimits(max_steps=50, max_tool_calls=100),
)
```

The `Agent` does not contain:

- current messages
- current run ID
- current session ID
- currently running tools
- current gateway
- currently executing tasks

Those belong elsewhere.

---

# 8. Model Initialization

Create a tiny model factory.

```python
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel


def build_model(model: str) -> BaseChatModel:
    return init_chat_model(
        model=model,
        temperature=0,
    )
```

Later:

```python
class ModelFactory:
    def create(self, config: ModelConfig) -> BaseChatModel:
        ...
```

Do not write custom adapters for every provider. The provider boundary belongs in `models/`.

Avoid code such as:

```python
if provider == "openai":
    ...
elif provider == "anthropic":
    ...
```

throughout your runtime. A provider-specific branch should only appear when the common model interface is genuinely insufficient.

---

# 9. Model Capability Validation

A model string is not enough. Your runtime needs to know whether a selected model supports what the agent needs.

Start with:

```python
@dataclass(frozen=True)
class ModelCapabilities:
    tool_calling: bool
    streaming: bool
    structured_output: bool = False
```

At startup, validate required capabilities. If the agent needs tool calling and the selected model cannot bind tools, fail during initialization rather than on the first user request.

---

# 10. Session Model

A session represents a durable conversational/work context.

```python
@dataclass
class Session:
    id: str
    agent_name: str
    owner_id: str
    title: str | None
    created_at: datetime
    updated_at: datetime
    status: SessionStatus
    metadata: dict[str, Any]
    version: int
```

Potential status values:

```text
ACTIVE
ARCHIVED
PAUSED
```

The key distinction is:

```text
Session
    = durable logical conversation

Run
    = one attempt at executing a request
```

---

# 11. Gateway-Agnostic Session Identity

Never use Telegram chat ID, browser socket ID, or TUI process ID as the actual session ID.

Instead:

```text
Gateway identity
       ↓
IdentityResolver
       ↓
principal_id
       ↓
SessionResolver
       ↓
session_id
```

Example:

```text
telegram:user:12345
        └── default session → uuid

web:user:abc123
        └── default session → same uuid

tui:local-user
        └── default session → same uuid
```

This is what enables one persistent session to move between Telegram, Web and TUI.

---

# 12. Run Model

Every user request should create a `Run`.

```python
@dataclass
class Run:
    id: str
    session_id: str
    agent_name: str
    status: RunStatus
    started_at: datetime
    finished_at: datetime | None
    step_count: int
    tool_call_count: int
    parent_run_id: str | None
    parent_tool_call_id: str | None
    error: str | None
    metadata: dict[str, Any]
```

Useful statuses:

```text
QUEUED
RUNNING
WAITING_APPROVAL
WAITING_INPUT
COMPLETED
FAILED
CANCELLED
TIMED_OUT
```

Run state is critical for long-horizon execution and recovery.

---

# 13. Agent State

Do not make `messages` the only state forever.

A reasonable model is:

```python
@dataclass
class AgentState:
    messages: list[BaseMessage]
    step_count: int
    tool_call_count: int
    todos: list[TodoItem]
    working_directory: str | None
    pending_approval: ApprovalRequest | None
    metadata: dict[str, Any]
```

For V1, however, only these are necessary:

```text
messages
step_count
tool_call_count
metadata
```

Add state only when the associated capability exists.

---

# 14. The Agent Loop

This is the heart of the system.

The initial implementation should be explicit.

```python
async def run(self, request: AgentRequest) -> AgentResult:
    state = await self.state_store.load(request.session_id)

    state.messages.append(
        HumanMessage(content=request.message)
    )

    while True:
        await self.check_limits(state)

        response = await self.model.ainvoke(
            self.build_messages(state)
        )

        state.messages.append(response)

        if not response.tool_calls:
            await self.state_store.save(state)
            return AgentResult(
                message=response,
                state=state,
            )

        tool_results = await self.tool_executor.execute_many(
            response.tool_calls,
            context=self.build_tool_context(state),
        )

        state.messages.extend(
            result.message for result in tool_results
        )

        await self.state_store.save(state)
```

That is the core. Do not hide it behind a large orchestration framework at the beginning.

---

# 15. Why the Explicit Loop Matters

DeepAgents and similar frameworks contain abstractions because they must support many configurations and execution modes.

Your personal agent has a different goal: understand the machinery.

An explicit loop teaches:

- state transitions
- model/tool boundaries
- tool execution
- concurrency
- persistence
- retries
- cancellation
- context limits
- subagent execution
- event streaming

When the loop becomes too complicated, extract stable concepts from it.

Do not invent those abstractions before duplication or complexity forces them.

---

# 16. Model Context Builder

The model should not receive the entire history forever.

Introduce:

```python
class ContextBuilder:
    async def build(
        self,
        state: AgentState,
        session: Session,
    ) -> list[BaseMessage]:
        ...
```

Conceptually:

```text
system prompt
      +
relevant persistent memory
      +
conversation summary
      +
recent messages
      +
recent tool results
```

Never treat internal execution events as ordinary model context.

---

# 17. System Prompt

Keep static instructions separate from dynamic runtime state.

Example:

```python
SYSTEM_PROMPT = """
You are my personal software agent.

You can inspect and modify files, run commands, use available tools,
and delegate specialized work to subagents.

Rules:
- inspect before changing
- prefer small, reversible changes
- verify changes with tests
- never claim success without evidence
- do not expose internal execution details
"""
```

Session-specific state belongs in a context builder, not in this constant.

---

# 18. Tool Registry

Create a single registry that resolves tool names.

```python
class ToolRegistry:
    def __init__(self, tools: Iterable[BaseTool]):
        self._tools = {tool.name: tool for tool in tools}

    def get(self, name: str) -> BaseTool:
        ...

    def all(self) -> list[BaseTool]:
        ...
```

Tool names must be unique. Reject collisions during agent construction.

---

# 19. Tool Context

Runtime-specific information should not become visible LLM arguments.

```python
@dataclass
class ToolContext:
    user_id: str
    session_id: str
    run_id: str
    workspace: Path
    cancellation: CancellationToken
    event_bus: EventBus
    artifact_store: ArtifactStore
```

The model should see `path`, `query`, `command`, etc. It should not see internal database connections, event buses, or secret configuration.

LangChain has injection mechanisms such as `InjectedToolArg`; use them where useful, but keep your internal runtime context separate from ordinary LLM-facing schemas.

---

# 20. Tool Executor

Do not let the agent loop directly own all tool execution concerns.

The runtime should call:

```text
ToolExecutor
    ↓
ToolRegistry
    ↓
BaseTool
```

The executor owns:

- lookup
- validation
- timeout
- cancellation
- concurrency
- policy checks
- error conversion
- event emission
- artifact handling

---

# 21. Parallel Tool Calls

Suppose a model requests three independent tools:

```text
read_file(a.py)
read_file(b.py)
git_status()
```

Execute them concurrently:

```python
results = await asyncio.gather(
    *[
        executor.execute(call, context=context)
        for call in tool_calls
    ]
)
```

Do not serialize independent operations unless there is a reason to.

However, concurrency needs a policy layer because two mutations may conflict.

---

# 22. Tool Concurrency Policy

Eventually associate runtime metadata with every tool.

```python
@dataclass(frozen=True)
class ToolPolicy:
    max_concurrency: int = 10
    requires_approval: bool = False
    mutates_state: bool = False
    concurrency_group: str | None = None
```

Examples:

```text
read_file:
    mutates_state = false
    concurrency_group = filesystem-read

write_file:
    mutates_state = true
    concurrency_group = filesystem-write

run_command:
    mutates_state = true
    concurrency_group = process

git_status:
    mutates_state = false

git_commit:
    mutates_state = true
```

Start simple. Add sophisticated scheduling only when a real conflict appears.

---

# 23. Tool Ordering Semantics

Completion order should never be used to identify a tool result.

If the model requests:

```text
A
B
C
```

and B completes first, the results must still contain:

```text
ToolMessage(tool_call_id=A)
ToolMessage(tool_call_id=B)
ToolMessage(tool_call_id=C)
```

Use `tool_call_id` as the association key and retain the original call ordering when constructing model messages.

---

# 24. Tool Errors

Recoverable tool errors should become tool messages rather than immediately terminating the entire agent.

```python
ToolMessage(
    content="Command failed: exit code 1\nstderr: ...",
    tool_call_id=call_id,
    status="error",
)
```

The model can then retry, inspect a different file, fix the command, or stop.

Actual run failure should be reserved for unrecoverable runtime/infrastructure conditions.

---

# 25. Tool Output Size

Never let tools dump arbitrary amounts of output into the model context.

Example initial limits:

```text
stdout: 32 KB
stderr: 32 KB
file read: configurable maximum
```

For large outputs:

```text
full output
    ↓
ArtifactStore
    ↓
small ToolMessage
```

This is essential for a coding agent that may execute commands repeatedly.

---

# 26. Artifact Store

Large files and tool outputs belong outside the database message payload.

```python
class ArtifactStore(Protocol):
    async def put(
        self,
        *,
        data: bytes,
        name: str,
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactRef:
        ...

    async def get(self, ref: ArtifactRef) -> bytes:
        ...
```

Start with the local filesystem:

```text
workspace/.agent/artifacts/<uuid>
```

Later you can add S3 or another backend without changing the runtime.

---

# 27. Terminal Tool

Terminal execution is a high-risk component and should be designed as a subsystem.

```python
@dataclass
class CommandResult:
    command: str
    cwd: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
```

A robust implementation should:

- use asynchronous subprocess APIs
- create a process group
- terminate child processes on cancellation
- enforce timeouts
- cap stdout/stderr
- preserve working directory
- use a controlled environment
- emit lifecycle events

Do not make `shell=True` implicit. If shell semantics are required, make that choice explicit.

---

# 28. Workspace Isolation

Give the agent a defined workspace.

Example:

```text
~/agent-workspaces/<session-id>/
```

Filesystem and terminal tools should use that root unless a deliberate policy allows external paths.

A useful policy type is:

```python
@dataclass(frozen=True)
class WorkspacePolicy:
    root: Path
    allow_external_paths: bool = False
```

---

# 29. Filesystem Tools

Start with:

```text
list_directory
read_file
write_file
edit_file
search_files
```

Do not build a huge filesystem API when the combination above plus a terminal tool already covers most coding workflows.

---

# 30. Never Echo Raw Reasoning / Internal Payloads

Your event system should expose safe, human-readable execution information rather than blindly serializing model responses.

Do not do this:

```python
response.model_dump()
```

for gateway output.

Instead project an explicit public event:

```python
AgentEvent(
    type="assistant.message",
    run_id=run_id,
    session_id=session_id,
    data={"text": visible_text},
)
```

Similarly, a tool event can say:

```text
Running tests
```

rather than exposing the complete internal tool call payload.

---

# 31. Event Model

Gateways should consume a runtime event protocol.

```python
class EventType(StrEnum):
    RUN_STARTED = "run.started"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    ASSISTANT_DELTA = "assistant.delta"
    ASSISTANT_MESSAGE = "assistant.message"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    SUBAGENT_STARTED = "subagent.started"
    SUBAGENT_COMPLETED = "subagent.completed"
    STATUS = "status"
    APPROVAL_REQUIRED = "approval.required"
    CHECKPOINT = "checkpoint"
```

```python
@dataclass(frozen=True)
class AgentEvent:
    id: str
    type: EventType
    run_id: str
    session_id: str
    sequence: int
    timestamp: datetime
    data: dict[str, Any]
```

---

# 32. Event Sequence Numbers

Sequence numbers make reconnection and replay possible.

Example:

```text
last_seen = 97

client reconnects
    ↓
request events > 97
    ↓
98, 99, 100, 101
```

This is valuable for Web, TUI and future background workers.

---

# 33. Event Bus

V1 can use an in-process queue.

```python
class EventBus:
    async def publish(self, event: AgentEvent) -> None:
        ...

    async def subscribe(
        self,
        run_id: str,
    ) -> AsyncIterator[AgentEvent]:
        ...
```

Later, the same conceptual interface can sit over Redis Streams, Postgres notifications, NATS, or another event system.

Do not introduce a distributed broker until multiple processes make it necessary.

---

# 34. Event Streaming vs Persistence

Streaming and persistence are different jobs.

Do not persist every token by default.

A better model is:

```text
assistant token
    ↓
EventBus
    ↓
gateway

final assistant message
    ↓
database
```

Semantic execution boundaries should be persisted; ephemeral token deltas do not need to be durable in V1.

---

# 35. Persistence Architecture

Separate three responsibilities:

## Session persistence

Stores:

- identity
- ownership
- title
- status

## Conversation persistence

Stores:

- human messages
- assistant messages
- tool messages

## Execution persistence

Stores:

- run status
- tool executions
- checkpoints
- approval requests
- progress

Avoid one giant JSON document as your database model. It becomes difficult to query, recover and migrate.

---

# 36. Database Schema

A reasonable SQLite schema is:

```text
users
-----
id
created_at
metadata_json

sessions
--------
id
owner_id
agent_name
title
status
version
created_at
updated_at
metadata_json

runs
----
id
session_id
parent_run_id
status
step_count
tool_call_count
started_at
finished_at
error
metadata_json

messages
--------
id
session_id
run_id
sequence
role
message_json
created_at

tool_executions
---------------
id
run_id
tool_call_id
tool_name
args_json
status
result_json
started_at
finished_at
duration_ms
error

checkpoints
-----------
id
run_id
session_id
sequence
state_json
created_at

memories
--------
id
owner_id
namespace
key
value_json
importance
created_at
updated_at

artifacts
---------
id
owner_id
session_id
run_id
name
path
mime_type
size_bytes
created_at

events
------
id
run_id
session_id
sequence
event_type
data_json
created_at
```

You do not need every table in Phase 1.

---

# 37. Message Persistence

Persist normalized message representations, not arbitrary Python objects.

For example:

```json
{
  "id": "msg-123",
  "type": "human",
  "content": "run the tests"
}
```

AI message with tool calls:

```json
{
  "id": "msg-124",
  "type": "ai",
  "content": "",
  "tool_calls": [
    {
      "id": "call-1",
      "name": "terminal",
      "args": {"command": "pytest"}
    }
  ]
}
```

Tool result:

```json
{
  "id": "msg-125",
  "type": "tool",
  "tool_call_id": "call-1",
  "content": "..."
}
```

Keep stable IDs so recovery and replay can reason about relationships.

---

# 38. Checkpoint Model

A checkpoint is a recoverable snapshot.

```python
@dataclass
class Checkpoint:
    id: str
    run_id: str
    session_id: str
    sequence: int
    state: AgentState
    created_at: datetime
```

Checkpoint at semantic boundaries such as:

```text
user message accepted
AI message received
tool batch completed
subagent completed
approval resolved
run completed
```

Do not checkpoint every token.

---

# 39. Resume Semantics

Consider a crash here:

```text
model call
    ↓
tool A completed
tool B completed
process crashes
```

On restart you need to determine whether those tools already completed.

Use:

```text
run_id + tool_call_id
```

as an idempotency identity.

Before executing a tool:

```text
already completed?
    yes → reuse result
    no  → execute
```

This is especially important for state-mutating operations.

---

# 40. Session Concurrency

Multiple gateways can race.

For example:

```text
Telegram: "continue"
Web:      "what's happening?"
TUI:      "run tests"
```

If they hit the same session simultaneously, you can corrupt ordering or run state.

For a single process, start with one `asyncio.Lock` per session. Later, replace that implementation with a database lease, advisory lock or distributed lock if the system becomes multi-process.

The interface should remain something like:

```python
class SessionLockManager(Protocol):
    async def acquire(self, session_id: str) -> AsyncContextManager[None]:
        ...
```

---

# 41. Optimistic Session Versioning

Also store a session version number.

For example:

```text
version = 41
```

Save using a compare-and-swap style update:

```sql
UPDATE sessions
SET version = 42
WHERE id = ?
  AND version = 41;
```

If the update changes zero rows, another writer modified the session. This prevents silent lost updates.

---

# 42. Cancellation

Every active run should carry cancellation state.

Conceptually:

```python
class CancellationToken:
    @property
    def cancelled(self) -> bool:
        ...

    async def wait(self) -> None:
        ...
```

Check cancellation:

```text
before model call
before tool execution
during long operations
after tool completion
before next model call
```

For subprocesses, cancel the process group rather than just the parent process.

---

# 43. Execution Limits

Long-horizon agents need hard boundaries.

```python
@dataclass(frozen=True)
class AgentLimits:
    max_steps: int = 50
    max_tool_calls: int = 100
    max_runtime_seconds: int = 1800
    max_concurrent_tools: int = 8
    max_subagent_depth: int = 2
    max_subagents: int = 8
```

Later add:

```text
max_model_tokens
max_tool_output_bytes
max_artifact_bytes
max_child_runs
```

A runaway loop must terminate predictably.

---

# 44. Retry Policy

Classify errors rather than retrying blindly.

```text
Transient:
    network timeout
    provider 429
    temporary DB failure

Retryable:
    provider 5xx
    temporary service unavailable

Non-retryable:
    invalid arguments
    permission denied
    command exited non-zero
    unknown tool

Fatal:
    corrupt state
    unrecoverable serialization failure
```

Use exponential backoff with jitter for transient provider/infrastructure errors.

Do not automatically rerun a mutating tool unless it has a clear idempotency mechanism.

---

# 45. Model Retries vs Tool Retries

Keep retry policies separate.

```text
ModelRetryPolicy
ToolRetryPolicy
PersistenceRetryPolicy
```

This avoids nested retries producing side effects multiple times.

---

# 46. Subagents

A subagent should still be an `Agent`.

```python
@dataclass(frozen=True)
class SubAgentSpec:
    name: str
    description: str
    agent: Agent
```

Example:

```python
researcher = SubAgentSpec(
    name="researcher",
    description="Researches technical topics and returns concise findings.",
    agent=research_agent,
)

coder = SubAgentSpec(
    name="coder",
    description="Implements and verifies code changes in the workspace.",
    agent=coding_agent,
)
```

---

# 47. Delegation Tool

Expose subagents to the parent through a normal tool:

```text
delegate(
    agent_name,
    task,
)
```

The runtime flow becomes:

```text
parent run
    ↓
delegation tool
    ↓
child run
    ↓
child agent
    ↓
child result
    ↓
ToolMessage to parent
```

The important design insight is that delegation can reuse the existing tool execution model, including concurrency, timeouts, event publication, and error handling.

---

# 48. Subagent Sessions

Every child should have its own execution context.

```text
parent session
   │
   ├── parent run
   │
   ├── child run: researcher
   │
   └── child run: coder
```

Do not dump the full parent conversation into every child.

Send only:

```text
task
relevant state
relevant artifacts
explicit constraints
```

---

# 49. Subagent Result

Return a compact result to the parent.

```python
@dataclass
class SubAgentResult:
    success: bool
    summary: str
    artifacts: list[ArtifactRef]
    facts: list[str]
    error: str | None
```

The parent receives the summary while the full child transcript remains in the child run.

This prevents child execution from exploding parent context length.

---

# 50. Parallel Subagents

If the parent emits:

```text
delegate(researcher, task A)
delegate(coder, task B)
```

then the normal `ToolExecutor.execute_many()` path can run them concurrently.

You do not need a second scheduling architecture solely for subagents.

Still enforce:

```text
max_subagent_depth
max_subagents
max_runtime
```

---

# 51. Shared Memory vs Shared Conversation

Subagents should share durable facts/artifacts, not blindly share the entire conversation.

```text
shared memory
    = durable facts / artifacts

private conversation
    = child run transcript
```

This separation is one of the most important context-scaling decisions in a multi-agent system.

---

# 52. Long-Horizon Tasks

Long horizon does not mean:

> Send a huge context window to the model forever.

It means:

> Represent work outside the immediate model context.

Use four mechanisms:

```text
1. persistent state
2. checkpoints
3. artifacts
4. summarization/compaction
```

A long task should look like:

```text
User goal
  ↓
session state
  ↓
short model context
  ↓
work artifacts
  ↓
checkpoint
  ↓
new context
  ↓
continue
```

---

# 53. Context Compaction

Introduce a context manager rather than putting compaction logic directly in the agent loop.

```python
class ContextManager:
    async def build_context(...):
        ...

    async def compact(...):
        ...
```

The first strategy can simply be:

```text
summary
+
recent messages
```

When history exceeds a threshold:

```text
old messages
    ↓
summarizer model
    ↓
summary
    +
recent messages
```

Do not compact every turn.

---

# 54. Summary Content

A useful summary should preserve:

- user goals
- decisions
- important constraints
- files changed
- outstanding tasks
- important tool results
- known failures
- unresolved questions

A weak summary:

```text
"The agent worked on a project."
```

A useful summary:

```text
"User is building a Python personal agent runtime using
init_chat_model and BaseTool. The runtime has an async model/tool loop,
parallel tool execution, and SQLite persistence. Remaining work is the
Telegram gateway. Current test failure is in test_resume because ..."
```

---

# 55. Planning / TODOs

Do not build a sophisticated planner first.

Start with:

```python
@dataclass
class TodoItem:
    id: str
    description: str
    status: TodoStatus
```

Statuses:

```text
PENDING
IN_PROGRESS
COMPLETED
BLOCKED
```

The model can use small tools such as:

```text
todo_list
todo_add
todo_complete
```

Build this after the base runtime works.

---

# 56. Memory

Separate two concepts:

## Short-term memory

```text
session state
conversation
current task
pending actions
```

## Long-term memory

```text
user preferences
project facts
persistent decisions
other information worth retaining across sessions
```

Do not put both in one undifferentiated `memory` object.

---

# 57. MemoryStore Interface

Start with a normal persistent key/value store.

```python
class MemoryStore(Protocol):
    async def get(self, owner_id: str, key: str) -> MemoryItem | None:
        ...

    async def set(
        self,
        owner_id: str,
        key: str,
        value: Any,
        *,
        namespace: str = "default",
    ) -> MemoryItem:
        ...

    async def delete(...):
        ...

    async def search(...):
        ...
```

For V1, `search()` can use SQL text/keyword matching.

Do not add vector embeddings until you actually need semantic retrieval.

---

# 58. Memory Tools

Start explicitly:

```text
remember(key, value)
recall(query)
forget(key)
```

Explicit memory operations are easier to understand and debug than automatic extraction.

Automatic memory can be added later as a separate feature.

---

# 59. Memory Namespaces

Use namespaces from the beginning:

```text
user
project
agent
session
```

Example:

```text
user.preferences
project.personal-agent
```

This prevents the memory store from becoming one giant dictionary.

---

# 60. Gateway Architecture

Define one internal inbound structure.

```python
@dataclass(frozen=True)
class MessageEnvelope:
    gateway: str
    external_user_id: str
    external_conversation_id: str
    text: str
    message_id: str
    metadata: dict[str, Any]
```

Each gateway converts its native protocol into this structure.

Then:

```text
MessageEnvelope
    ↓
IdentityResolver
    ↓
SessionResolver
    ↓
AgentRuntime
```

---

# 61. Gateway Output

Gateways should consume events, not inspect internal model/tool messages.

```text
AgentEvent stream
```

Example:

```text
run.started
status: "Inspecting the repository"

tool.started
status: "Running tests"

tool.completed
status: "Tests completed"

subagent.started
status: "Delegating code inspection"

assistant.delta
text: "I found ..."

run.completed
```

Each gateway can render these differently.

---

# 62. Web Gateway

A simple initial API is:

```text
POST /sessions
GET  /sessions
POST /sessions/{session_id}/messages
GET  /sessions/{session_id}
GET  /runs/{run_id}/events
POST /runs/{run_id}/cancel
```

Use SSE as the first streaming mechanism because it is simple for a server-to-client event stream. WebSocket can be added later for bidirectional interactive approval/cancellation flows.

---

# 63. Web API Model

Request:

```json
{
  "session_id": "uuid",
  "message": "run the tests"
}
```

Response:

```json
{
  "run_id": "uuid",
  "session_id": "uuid",
  "status": "running"
}
```

Then:

```text
GET /runs/{run_id}/events
```

streams events.

This decouples request creation from long-running execution.

---

# 64. Telegram Gateway

Treat Telegram as an adapter.

```text
Telegram update
    ↓
MessageEnvelope
    ↓
application service
    ↓
run
    ↓
events
    ↓
Telegram renderer
```

Do not put tool execution, model calls, or session persistence inside Telegram code.

---

# 65. Telegram Rendering

Telegram should receive a compact presentation.

Example:

```text
Working...

[tool] Running tests
[tool] Inspecting src/runtime.py
[agent] Found a state-resume issue

Final:
The issue was ...
```

Use debouncing/aggregation rather than editing a message for every token.

---

# 66. TUI Gateway

The TUI can expose more execution detail than Telegram.

```text
┌─────────────────────────────────────────────────────────┐
│ Personal Agent — session abc123                        │
├─────────────────────────────────────────────────────────┤
│ User                                                    │
│ Fix the failing tests                                   │
│                                                         │
│ Agent                                                   │
│ Inspecting repository...                                │
│                                                         │
│ Tools                                                   │
│ ✓ git status                                            │
│ ✓ pytest                                                │
│ → read_file tests/test_runtime.py                       │
│                                                         │
│ Output                                                  │
│ I found the failure in ...                              │
├─────────────────────────────────────────────────────────┤
│ >                                                       │
└─────────────────────────────────────────────────────────┘
```

The TUI consumes the same event stream as Web and Telegram.

---

# 67. Cross-Gateway Persistence

All gateways should resolve to the same `Session` abstraction.

Example:

```text
Telegram
    ↓
existing session
    ↓
"continue the task"

Web
    ↓
same session
    ↓
"what did you change?"

TUI
    ↓
same session
    ↓
"run the tests"
```

There should be no separate `TelegramSession` or `WebSession` in the core domain.

---

# 68. Application Service Layer

Gateways should not call the runtime with ad-hoc logic.

Use:

```python
class AgentApplication:
    async def send_message(self, envelope: MessageEnvelope) -> RunHandle:
        ...

    async def cancel_run(self, run_id: str, principal_id: str) -> None:
        ...

    async def get_session(self, session_id: str, principal_id: str) -> Session:
        ...
```

This becomes the stable application boundary.

---

# 69. Dependency Injection

Assemble infrastructure once.

```python
@dataclass
class Application:
    agent_registry: AgentRegistry
    session_store: SessionStore
    run_store: RunStore
    checkpoint_store: CheckpointStore
    memory_store: MemoryStore
    event_bus: EventBus
    artifact_store: ArtifactStore
```

Gateways receive the application rather than constructing their own storage/runtime objects.

---

# 70. Agent Registry

If you expect multiple agent definitions, keep a small registry:

```python
class AgentRegistry:
    def register(self, agent: Agent) -> None:
        ...

    def get(self, name: str) -> Agent:
        ...
```

Example agents:

```text
personal
coder
researcher
```

The personal agent can use coder and researcher as subagents.

---

# 71. Agent Runtime vs Agent Definition

Definition:

```python
personal_agent = Agent(...)
```

Runtime:

```python
runtime = AgentRuntime(
    agent=personal_agent,
    ...
)
```

The separation matters because an Agent definition is reusable configuration, while runtime dependencies and execution are tied to infrastructure and active runs.

---

# 72. Agent Runtime Interface

A clean first interface:

```python
class AgentRuntime:
    async def run(
        self,
        *,
        session_id: str,
        message: str,
        user_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> RunHandle:
        ...

    async def resume(self, run_id: str) -> RunHandle:
        ...

    async def cancel(self, run_id: str) -> None:
        ...
```

`RunHandle` can expose:

```python
class RunHandle:
    run_id: str
    session_id: str

    async def events(self):
        ...

    async def result(self):
        ...
```

---

# 73. Invoke vs Stream

Do not implement a separate execution engine for invocation and streaming.

Use one execution path:

```text
run()
  ├── publishes events
  └── stores final state
```

Then:

```text
invoke()
    = wait for completion

stream()
    = consume events while running
```

This avoids duplicate behavior.

---

# 74. Background vs Foreground Runs

Gateways should not have to hold an HTTP request open for an hour-long task.

Use:

```text
request
  ↓
create run
  ↓
run worker
  ↓
subscribe to events
```

V1 can use `asyncio.create_task()`.

Later:

```text
RunQueue
   ↓
Worker
   ↓
AgentRuntime
```

Keep the external API the same.

---

# 75. Run Manager

Introduce a small orchestration object once runs become first-class:

```python
class RunManager:
    async def start(self, request: RunRequest) -> RunHandle:
        ...

    async def resume(self, run_id: str) -> RunHandle:
        ...

    async def cancel(self, run_id: str) -> None:
        ...

    async def recover(self) -> None:
        ...
```

`recover()` is important for long-running tasks after application restart.

---

# 76. Application Startup Recovery

On startup:

```text
application starts
      ↓
database ready
      ↓
find incomplete runs
      ↓
inspect last checkpoints and tool records
      ↓
resume safe runs / mark unrecoverable runs
      ↓
start gateways
```

Do not blindly rerun all incomplete tools.

---

# 77. State Machine

Even without LangGraph, make execution states explicit.

```text
CREATED
  ↓
RUNNING_MODEL
  ↓
MODEL_RETURNED
  ├── final → COMPLETED
  │
  └── tools
       ↓
RUNNING_TOOLS
       ↓
TOOLS_COMPLETED
       ↓
CHECKPOINTING
       ↓
RUNNING_MODEL
```

Approval:

```text
RUNNING_TOOLS
    ↓
APPROVAL_REQUIRED
    ↓
WAITING_APPROVAL
    ↓
APPROVED → RUNNING_TOOLS
    ↓
REJECTED → model receives rejection result
```

Cancellation must be possible from most active states.

---

# 78. Approval System

For dangerous tools such as terminal operations, add an approval object.

```python
@dataclass
class ApprovalRequest:
    id: str
    run_id: str
    tool_name: str
    arguments: dict[str, Any]
    reason: str
    status: ApprovalStatus
```

Emit:

```text
approval.required
```

The gateway renders the approval mechanism.

---

# 79. Approval Policy

Start with a simple `requires_approval` flag, then evolve toward classification.

Potentially sensitive commands include:

```text
rm
rm -rf
git reset --hard
git clean
chmod
package installation
network-sensitive actions
credential operations
```

Do not rely on one giant regex forever. Use policy + tool metadata + explicit allow/deny controls.

---

# 80. Security Boundary

Even a personal agent should assume the model can make a mistake.

At minimum:

```text
workspace restriction
environment filtering
command timeout
output limits
approval hooks
credential isolation
```

Unrestricted terminal + unrestricted filesystem + unrestricted credentials is a poor default.

---

# 81. Secrets

Do not expose the full process environment to tool execution by default.

Prefer an allowlist such as:

```text
PATH
HOME
LANG
```

Add only required variables.

Do not let arbitrary model-visible output contain:

```text
API keys
cloud credentials
database passwords
Telegram token
SSH private keys
```

---

# 82. Tool Result Redaction

Tool output can contain secrets.

Create a redaction layer before model-visible output:

```python
safe_content = redactor.redact(raw_content)
```

Store full output as an artifact if it is needed for debugging.

---

# 83. MCP Integration

Do not implement MCP yourself initially.

Treat MCP-provided tools as normal tools:

```text
MCP server
    ↓
MCP adapter
    ↓
BaseTool
    ↓
ToolRegistry
    ↓
Agent
```

The runtime should not care whether a tool came from your Python code, MCP, or another future source.

---

# 84. Tool Metadata

Keep LangChain's `BaseTool` as the actual model-facing tool abstraction, and add your own runtime metadata around it.

```python
@dataclass(frozen=True)
class RegisteredTool:
    tool: BaseTool
    policy: ToolPolicy
    timeout_seconds: float
    tags: frozenset[str]
```

This is a good example of adding domain-specific information without duplicating LangChain's tool interface.

---

# 85. Tool Lifecycle

Every execution follows a consistent lifecycle:

```text
resolve
  ↓
validate
  ↓
authorize
  ↓
emit started
  ↓
execute
  ↓
capture output
  ↓
store artifact if required
  ↓
build ToolMessage
  ↓
emit completed
```

On failure:

```text
emit failed
  ↓
ToolMessage(status="error")
```

This is much easier to test than putting all of the logic inside individual tools.

---

# 86. ToolExecutionResult

Separate the model-visible `ToolMessage` from the runtime-level result.

```python
@dataclass
class ToolExecutionResult:
    tool_call_id: str
    tool_name: str
    message: ToolMessage
    success: bool
    duration_ms: int
    artifact_refs: list[ArtifactRef]
    error: str | None
```

This gives you a clean projection:

```text
ToolExecutionResult
        │
        ├── ToolMessage → model
        │
        └── AgentEvent → gateway
```

---

# 87. Observability

At minimum log:

```text
run_id
session_id
agent_name
model
step
tool_name
tool_call_id
duration
status
```

Never log secrets or raw credential-bearing command output.

Use structured JSON logs so filtering by `run_id` or `tool_name` works later.

---

# 88. Metrics

Useful initial metrics:

```text
agent_runs_total
agent_runs_failed_total
model_calls_total
model_call_duration_seconds
tool_calls_total
tool_call_duration_seconds
tool_failures_total
subagent_runs_total
context_compactions_total
```

Token/cost accounting can come later and should remain separate from the runtime's core semantics.

---

# 89. Tracing

Give every run a trace identity.

Conceptually:

```text
run
 ├── model call
 ├── tool call
 │    └── subprocess
 ├── model call
 └── subagent run
      ├── model
      └── tools
```

This is extremely useful when a long-horizon task takes many minutes and multiple tools/subagents are involved.

---

# 90. Testing Philosophy

The runtime should be testable without real LLM calls.

Build deterministic fake models that produce scripted responses.

Example scenario:

```text
Input:
"Read a.txt and b.txt"

Fake model:
tool_call read_file(a.txt)
tool_call read_file(b.txt)

Executor:
returns contents

Fake model:
"Both files contain ..."
```

This lets you test the runtime itself without spending money or depending on provider latency.

---

# 91. Test Layers

## Unit tests

Test:

- ToolRegistry
- ToolExecutor
- state transitions
- limits
- cancellation
- serialization
- session resolution
- event ordering

## Integration tests

Test:

- real LangChain model
- SQLite persistence
- real terminal tool in a temporary workspace
- subagent execution

## End-to-end tests

Test:

```text
gateway
 → application
 → runtime
 → persistence
 → tool
 → events
 → gateway output
```

Do not make every test an LLM test.

---

# 92. Critical Runtime Invariants

Write these as actual tests.

## Invariant 1

Every executed tool call has one matching `ToolMessage`.

```text
AIMessage.tool_calls[i].id
        ↕
ToolMessage.tool_call_id
```

## Invariant 2

A session cannot have multiple active runs unless you explicitly support concurrent session turns.

## Invariant 3

A completed run is immutable.

## Invariant 4

Messages are append-oriented; edits are explicit operations, not silent overwrites.

## Invariant 5

Mutating tool execution has an idempotency identity.

## Invariant 6

The agent eventually exits via final response, limit, cancellation, approval wait, or fatal failure.

## Invariant 7

Gateways never execute tools directly.

## Invariant 8

Core runtime code never imports Telegram, FastAPI, or TUI-specific code.

---

# 93. Error Taxonomy

Use a small explicit hierarchy:

```python
class AgentError(Exception):
    pass


class AgentConfigError(AgentError):
    pass


class ModelError(AgentError):
    pass


class ToolError(AgentError):
    pass


class ToolNotFoundError(ToolError):
    pass


class ToolTimeoutError(ToolError):
    pass


class ToolPolicyError(ToolError):
    pass


class PersistenceError(AgentError):
    pass


class RunCancelled(AgentError):
    pass


class RunLimitExceeded(AgentError):
    pass
```

The point is to distinguish recoverable user/tool errors from infrastructure failures.

---

# 94. API Boundaries

Keep the dependency direction clear:

```text
gateway
   ↓
application
   ↓
core
   ↓
interfaces
   ↑
implementations
```

The runtime should never import a gateway package.

---

# 95. Dependency Inversion

The core should depend on protocols for infrastructure.

Example:

```python
class SessionStore(Protocol):
    async def get(self, session_id: str) -> Session | None:
        ...

    async def save(self, session: Session) -> None:
        ...
```

Then:

```text
AgentRuntime
    ↓
SessionStore
    ↑
SqliteSessionStore
```

Later:

```text
PostgresSessionStore
```

with no runtime rewrite.

---

# 96. Serialization Boundary

Keep representations separate.

Inside core:

```text
Session
Run
AgentState
ArtifactRef
MemoryItem
AgentEvent
```

At persistence boundary:

```text
JSON / SQL primitives
```

At gateway boundary:

```text
HTTP JSON / Telegram objects / TUI objects
```

Do not let framework-specific objects leak everywhere.

---

# 97. Configuration

Use typed configuration.

```python
@dataclass(frozen=True)
class AppConfig:
    database_url: str
    workspace_root: Path
    default_model: str
    max_concurrent_tools: int
    telegram_token: str | None
```

Do not scatter `os.getenv()` throughout the codebase.

---

# 98. Dependency Bootstrap

Assemble dependencies in one place:

```python
def create_application(config: AppConfig) -> Application:
    db = create_database(config)

    session_store = SqliteSessionStore(db)
    run_store = SqliteRunStore(db)
    checkpoint_store = SqliteCheckpointStore(db)
    memory_store = SqliteMemoryStore(db)

    artifact_store = LocalArtifactStore(
        config.workspace_root / ".agent" / "artifacts"
    )

    event_bus = EventBus()

    agents = create_agents(...)

    return Application(...)
```

Keep assembly code out of the core runtime.

---

# 99. Do Not Overuse Dependency Injection

For a personal application, this is usually unnecessary:

```text
Container
Provider
FactoryFactory
RuntimeBuilder
```

Prefer explicit constructors.

```python
runtime = AgentRuntime(
    agent=agent,
    session_store=session_store,
    run_store=run_store,
    checkpoint_store=checkpoint_store,
    event_bus=event_bus,
    artifact_store=artifact_store,
)
```

Readable explicit wiring is a strength here.

---

# 100. Agent Constructor Recommendation

The constructor you originally described is close to the right one.

Recommended V1:

```python
Agent(
    name: str,
    model: str | BaseChatModel,
    tools: Sequence[BaseTool] = (),
    subagents: Sequence[SubAgentSpec] = (),
    system_prompt: str = "",
    limits: AgentLimits | None = None,
)
```

Do **not** make `state` a constructor argument.

Do **not** make persistent `memory` a mutable object owned by the Agent.

Those belong to runtime/session infrastructure.

---

# 101. Where Memory Belongs

Rather than making the Agent own concrete memory infrastructure:

```python
Agent(..., memory=...)
```

prefer:

```python
AgentRuntime(
    agent=agent,
    memory_store=memory_store,
)
```

The Agent can describe that it has memory-related tools/capabilities, while storage remains infrastructure.

---

# 102. Public API

You can expose a friendly wrapper later:

```python
class PersonalAgent:
    async def ask(self, text: str) -> AgentResult:
        ...

    async def stream(self, text: str) -> AsyncIterator[AgentEvent]:
        ...
```

Internally:

```text
PersonalAgent
    ↓
SessionResolver
    ↓
AgentRuntime
```

This gives you a clean user API without turning the Agent object itself into a god object.

---

# 103. Cross-Gateway Authorization

Every persistent session should have an owner identity.

```text
session.owner_id
```

Every gateway request should resolve to:

```text
principal_id
```

Then the application layer checks ownership before loading/streaming a session or run.

Even if the application is private today, this is a cheap and useful boundary.

---

# 104. Gateway Reconnection

Web and TUI should support reconnect:

```text
connect
 ↓
provide run_id
 ↓
provide last_event_sequence
 ↓
replay missed events
 ↓
continue live subscription
```

This becomes almost free once events have sequence numbers.

---

# 105. Idempotent Gateway Messages

Every external message should have an idempotency key.

For Telegram, for example:

```text
gateway = telegram
external_message_id = 12345
```

Store the mapping:

```text
gateway
external_message_id
session_id
run_id
```

When Telegram retries the same update, return/reuse the original run rather than duplicating it.

---

# 106. Exactly-Once Reality

Do not promise true exactly-once behavior for arbitrary tools.

The practical model is:

```text
at-least-once execution
+
idempotent side-effect handling
```

For example, a DB insert can use an idempotency key. A generic email send may require provider-level idempotency support.

Design your tools with this reality in mind.

---

# 107. Performance Principles

The runtime should be async-first.

Avoid blocking calls in async paths:

```python
subprocess.run(...)
time.sleep(...)
requests.get(...)
```

Use async-native implementations or `asyncio.to_thread()` where a sync dependency is unavoidable.

---

# 108. Reuse Model Instances

Prefer a shared model object per configured Agent definition:

```python
model = build_model(...)
agent = Agent(model=model)
```

rather than constructing a model client on every turn.

---

# 109. Bounded Concurrency

Use a global concurrency limit and optional per-tool limits.

```python
asyncio.Semaphore(8)
```

Otherwise a large model tool-call batch could spawn too many subprocesses/API requests and make the entire application unstable.

---

# 110. Database Connection Pooling

SQLite can remain simple for V1.

When moving to PostgreSQL, use a proper async connection pool and reuse connections.

Do not create a database connection per message or per tool execution.

---

# 111. Context Efficiency

Optimize context in this order:

```text
1. do not include irrelevant tool output
2. do not keep old messages unnecessarily
3. summarize old history
4. store large data as artifacts
5. retrieve memory selectively
```

Sending an unnecessarily large prompt will often cost more time than micro-optimizing Python code.

---

# 112. Tool Result Summarization

For example, a `pytest` command might generate thousands of lines.

The model may only need:

```text
12 tests passed
2 tests failed
first failure:
tests/test_runtime.py::test_resume
AssertionError: ...
```

while the full log remains in an artifact.

This is a major source of performance improvement in coding agents.

---

# 113. Coding Agent Tool Set

Start with:

```text
list_directory
read_file
write_file
edit_file
search_files
run_command
git_status
git_diff
```

Later:

```text
git_log
git_show
git_commit
apply_patch
web_search
```

Do not expose every Unix command as its own tool.

---

# 114. Coding Agent Behavioral Contract

The coding subagent's prompt should encourage:

```text
1. Understand before modifying.
2. Inspect relevant files.
3. Make minimal changes.
4. Run targeted tests.
5. Inspect failures.
6. Fix them.
7. Run broader tests.
8. Report exact changes and verification.
```

These are prompt-level behaviors, not runtime mechanics.

---

# 115. Research Agent

A useful second subagent is a researcher with:

```text
web_search
web_fetch
```

and an output shape like:

```text
summary
sources
facts
open_questions
```

The parent receives a compact result.

---

# 116. Model Routing

Do not add routing initially.

Start with:

```text
one Agent
one model
```

Later, when usage shows a real need, add:

```text
fast model
reasoning model
coding model
```

and a small router.

---

# 117. Model-Specific Configuration

Keep provider-specific settings isolated under `models/`.

For example:

```python
ModelConfig(
    model="provider:model",
    kwargs={...},
)
```

The runtime should not branch on provider unless required by a genuine capability mismatch.

---

# 118. Structured Output

Do not require every model response to be a Pydantic object.

Your minimal agent only needs:

```text
AIMessage
ToolCall
ToolMessage
```

Structured output can later be added to narrow tasks such as memory extraction, planning, and subagent results.

---

# 119. Message Abstraction

Because you want to use LangChain messages, keep them at the model boundary:

```text
SystemMessage
HumanMessage
AIMessage
ToolMessage
```

Do not invent parallel message classes unless you later need a separate domain representation for a concrete reason.

---

# 120. Your Own Domain Types

Create your own types for things LangChain does not own:

```text
Agent
AgentLimits
SubAgentSpec
AgentState
Session
Run
AgentEvent
ToolExecutionResult
ArtifactRef
MemoryItem
ApprovalRequest
MessageEnvelope
```

Avoid duplicating the standard model message hierarchy.

---

# 121. Message vs Event

Keep these distinctions explicit:

```text
BaseMessage
    = model context

AgentEvent
    = runtime/gateway protocol

Database row
    = persistence representation
```

For example:

```text
ToolMessage:
    model-facing result

AgentEvent:
    "Running tests"

Database:
    tool execution row + serialized result
```

---

# 122. Session Summary vs Run Summary

A run result might be:

```text
"Fixed test failure in runtime.py."
```

The broader session may preserve:

```text
"The project uses an async agent loop, SQLite persistence, a
parallel tool executor, and a Telegram gateway. Current unfinished
work is approval handling."
```

Keep those concepts separate.

---

# 123. Artifact References

The model should receive compact artifact references instead of embedded binary payloads.

For example:

```text
artifact://abc123
```

A gateway can later turn that into:

```text
Open file
Download output
View diff
```

---

# 124. Human-in-the-Loop Flow

The generic approval flow is:

```text
agent
 ↓
tool call
 ↓
policy
 ↓
approval required
 ↓
run = WAITING_APPROVAL
 ↓
approval.required event
 ↓
gateway renders approval
 ↓
user approves
 ↓
application resumes run
 ↓
tool executes
```

The runtime never needs to know which gateway provided the approval.

---

# 125. Approval Security

When an approval is resolved, validate:

```text
approval_id
run_id
session_id
principal_id
```

Do not allow a generic `/approve` route with no binding to the specific pending action.

---

# 126. Gateway-Specific Rate Limits

Core runtime limits are not enough.

Gateways can also enforce:

```text
messages/minute
concurrent runs
maximum message size
```

This belongs near the gateway/application layer, not deep inside tools.

---

# 127. Tool Rate Limits

Later some tools may need their own:

```text
web_search → N/min
API calls → provider-specific quota
terminal → N concurrent processes
```

Represent such policy as metadata rather than hard-coding it into the Agent loop.

---

# 128. Database Choice

For your use case:

## Phase 1

SQLite.

Why:

- no operational overhead
- durable across restarts
- simple backup
- easy debugging
- adequate for a personal agent

## Phase 2

PostgreSQL when you need:

- multiple worker processes
- stronger locking/concurrency
- remote operation
- better operational tooling

Do not use a distributed database solely because the system is called an agent.

---

# 129. Database Access

Keep SQL behind repository interfaces.

For example:

```python
class RunStore(Protocol):
    async def create(self, run: Run) -> None:
        ...

    async def get(self, run_id: str) -> Run | None:
        ...

    async def update(self, run: Run) -> None:
        ...
```

Use an existing DB abstraction where appropriate; do not write your own ORM.

---

# 130. Transactions

Group related state changes into explicit transactional boundaries.

For example:

```text
tool batch completed
      ↓
transaction
    store tool results
    append tool messages
    update run state
    create checkpoint
commit
```

This makes recovery and consistency much easier to reason about.

---

# 131. Background Scheduling

Do not build a scheduler before execution recovery works.

The first milestone should be:

```text
run
resume
cancel
persist
```

Then:

```text
scheduler
    ↓
RunManager.start()
```

---

# 132. Future Distributed Architecture

If the single process becomes too small:

```text
Gateway
   ↓
Application
   ↓
RunQueue
   ↓
Worker 1 / Worker 2 / Worker 3
   ↓
AgentRuntime
   ↓
PostgreSQL
```

Because runs and checkpoints are already durable concepts, this transition should be incremental.

---

# 133. Why Not Start With LangGraph?

For your learning goal, I recommend not using LangGraph as the initial execution engine.

An explicit custom loop forces you to understand:

- state transitions
- persistence
- checkpointing
- cancellation
- parallel tool execution
- subagent boundaries
- resume semantics

LangGraph is still highly relevant later. Its current documentation separates thread-scoped checkpoints from cross-thread stores, and `StateGraph` provides graph execution and streaming capabilities.

Use it later when:

```text
custom runtime
        ↓
starts becoming harder to reason about than the problems it solves
```

not merely because DeepAgents uses it.

---

# 134. If You Later Use LangGraph

The concepts map roughly as:

```text
your Session
    ≈ LangGraph thread

your CheckpointStore
    ≈ LangGraph checkpointer

your MemoryStore
    ≈ LangGraph store

your AgentState
    ≈ graph state

your AgentRuntime
    ≈ graph execution
```

Understanding your own model first will make the LangGraph model much easier to learn later.

---

# 135. Avoid Premature Middleware

Repeated cross-cutting behavior may eventually justify middleware:

```text
logging
retry
approval
summarization
memory injection
tool filtering
```

But start with direct code. When duplication becomes real, extract the abstraction.

Potential future middleware:

```text
RunLimitMiddleware
LoggingMiddleware
ToolPolicyMiddleware
ApprovalMiddleware
ContextCompactionMiddleware
MemoryMiddleware
ModelRetryMiddleware
```

These should be optional behaviors, not mandatory framework complexity.

---

# 136. Initial README / Design Journal

Keep a learning-oriented project structure:

```text
docs/
  decisions/
  experiments/
  architecture/
```

Examples:

```text
docs/decisions/001-agent-vs-session.md
docs/decisions/002-custom-loop.md
docs/experiments/tool-parallelism.md
```

This is particularly valuable because your goal is not only to ship a tool, but to relearn architecture and systems concepts.

---

# 137. Testing the Central Loop

Write tests that read like a specification:

```text
test_returns_direct_model_response

test_executes_single_tool_call

test_executes_parallel_tool_calls

test_preserves_tool_call_ids

test_continues_after_tool_error

test_stops_after_max_steps

test_stops_when_cancelled

test_persists_messages

test_resumes_from_checkpoint

test_does_not_duplicate_completed_tool

test_runs_subagent

test_enforces_subagent_depth
```

The central runtime tests matter more than surface-level gateway tests.

---

# 138. Parallelism Test

Build a fake tool that sleeps for a known interval.

Run:

```text
tool A = 1s
tool B = 1s
```

Then verify the combined elapsed time is close to 1 second rather than 2 seconds.

This turns concurrency from an assumption into a tested invariant.

---

# 139. Resume Test

Simulate:

```text
model
 → tool A
 → checkpoint
 → crash
```

Then create a new runtime and call `resume()`.

Expected:

```text
tool A is not duplicated
model receives existing result
run continues
final result is persisted
```

This test should exist before you claim long-horizon reliability.

---

# 140. Cross-Gateway Test

End-to-end scenario:

```text
Telegram:
create session
send: "My project uses FastAPI."

↓

Web:
open same session
ask: "What framework does my project use?"

↓

TUI:
ask: "Summarize our conversation."
```

Expected:

```text
same session
same messages
same identity
```

---

# 141. Long-Horizon Test

Artificially create a 100-step task and verify:

```text
checkpoints are created
context is compacted
large outputs become artifacts
run can resume
limits stop runaway behavior
```

Do this with fake models rather than waiting for a real task to take hours.

---

# 142. Tool Output and Context Performance

Measure:

```text
prompt bytes
prompt token count
model latency
tool latency
checkpoint latency
```

You will learn quickly whether a slow task is actually caused by the model, the tools, the database, or simply excessive context.

---

# 143. Initial Technology Stack

Minimal:

```text
Python 3.12+
uv
LangChain / langchain-core
SQLite
FastAPI
```

Likely later:

```text
Textual
python-telegram-bot
Pydantic
SQLAlchemy / SQLModel
pytest
pytest-asyncio
ruff
pyright
```

Install packages as features are implemented rather than all at once.

---

# 144. Development Commands

A useful baseline is:

```bash
uv run pytest
uv run ruff check .
uv run ruff format .
uv run pyright
```

Keep these fast enough to run frequently.

---

# 145. Dependency Philosophy

Do not rewrite mature infrastructure simply for educational value.

Reasonable to reuse:

- model clients
- HTTP protocol libraries
- Telegram client libraries
- SQL drivers
- MCP adapters
- serialization libraries

The learning target is your agent architecture, not reproducing every ecosystem library.

---

# 146. Session Commands

Gateways can have different presentation-level commands, but translate them to internal application commands.

Telegram:

```text
/new
/sessions
/cancel
```

TUI:

```text
Ctrl+L
Ctrl+C
```

Web:

```text
buttons
```

All can map to the same application operations:

```text
CreateSession
ListSessions
CancelRun
SwitchSession
```

---

# 147. Session Titles

A session title is a useful convenience but should not require an additional model call in V1.

Use a heuristic such as the first meaningful user message truncated to a safe length.

A model-generated title can be added later.

---

# 148. Data Retention

Eventually define how long you retain:

```text
messages
runs
events
raw tool artifacts
memories
```

A sensible starting policy is:

```text
messages → long-lived
events → bounded retention
raw tool outputs → configurable
memories → explicit deletion
artifacts → configurable / user-controlled
```

---

# 149. Deletion Semantics

Support explicit deletion eventually:

```text
delete session
delete memory
delete artifact
```

Define whether deleting a session also deletes:

```text
runs
messages
events
artifacts
```

Do not accidentally leave large orphaned artifacts behind.

---

# 150. Naming Conventions

Prefer concrete names:

```text
Agent
AgentRuntime
Session
Run
ToolRegistry
ToolExecutor
MemoryStore
ArtifactStore
CheckpointStore
EventBus
SessionResolver
ContextBuilder
RunManager
```

Avoid vague classes such as:

```text
Helper
Utility
Manager
Processor
Handler
```

unless they represent a clear domain concept.

---

# 151. What You Should Not Abstract

Do not create:

```text
UniversalMessage
UniversalTool
UniversalModel
```

and do not hide all of:

```python
model.bind_tools(...)
```

behind arbitrary layers.

You specifically want to understand LangChain's primitives, so use them directly at the model boundary.

---

# 152. What You Should Abstract Early

These are good early boundaries because their implementations are likely to change:

```text
SessionStore
RunStore
CheckpointStore
MemoryStore
ArtifactStore
ToolRegistry
EventBus
```

These are infrastructure boundaries, not artificial abstractions.

---

# 153. State Ownership

Be explicit about ownership:

```text
Agent
    owns configuration

Session
    owns durable conversation identity / metadata

Run
    owns execution lifecycle

AgentState
    owns current execution state

ToolExecutor
    owns tool execution details

MemoryStore
    owns cross-session memories

ArtifactStore
    owns large outputs/files

EventBus
    owns runtime event distribution
```

Avoid two components both mutating the same conceptual state.

---

# 154. Avoid Mutable Global State

Bad:

```python
CURRENT_SESSION = ...
CURRENT_RUN = ...
```

Good:

```python
ToolContext(...)
AgentState(...)
Run(...)
```

Pass execution context explicitly.

---

# 155. Immutable Configuration

Configuration types should preferably be frozen:

```python
@dataclass(frozen=True)
class AgentLimits:
    ...

@dataclass(frozen=True)
class SubAgentSpec:
    ...
```

Mutable information belongs to sessions and runs.

---

# 156. Async-First API

Because the system must support gateways, concurrency and long-running tasks:

```python
await runtime.run(...)
await store.get(...)
await store.save(...)

async for event in runtime.stream(...):
    ...
```

This should be the default design direction.

---

# 157. Do Not Stream Everything

Meaningful events:

```text
assistant text
tool started
tool completed
subagent started
subagent completed
approval request
status
```

Do not make every internal mutation a user-facing event.

Each gateway can choose which events to display.

---

# 158. Human-Friendly Status

Useful runtime-generated messages include:

```text
Inspecting the project
Reading configuration
Running tests
Checking the failing file
Delegating research
Waiting for approval
```

These should be based on tool metadata, runtime state and gateway presentation policy rather than raw hidden reasoning content.

---

# 159. Gateway Rendering Policy

Different gateways can render the same event differently.

For example:

```text
Web:
    show live stream + detailed tool panel

TUI:
    show live execution tree

Telegram:
    collapse events into short status updates
```

The event protocol remains identical.

---

# 160. Recommended Agent Definition API

Your original API idea is sound with two changes:

1. Keep state out of `Agent`.
2. Keep concrete memory storage out of `Agent`.

Recommended:

```python
personal = Agent(
    name="personal",
    model="openai:gpt-5.5",
    tools=[
        terminal,
        read_file,
        write_file,
        search_files,
    ],
    subagents=[
        researcher,
        coder,
    ],
    system_prompt=SYSTEM_PROMPT,
    limits=AgentLimits(
        max_steps=50,
        max_tool_calls=100,
        max_runtime_seconds=1800,
    ),
)
```

Then infrastructure is supplied externally:

```python
runtime = AgentRuntime(
    agent=personal,
    session_store=session_store,
    run_store=run_store,
    checkpoint_store=checkpoint_store,
    memory_store=memory_store,
    artifact_store=artifact_store,
    event_bus=event_bus,
)
```

This is the cleanest separation for your stated goal.

---

# 161. Proposed Final Dependency Graph

```text
                           ┌───────────────┐
                           │    Gateway    │
                           │ Web/TUI/TG     │
                           └───────┬───────┘
                                   │
                                   ▼
                         ┌──────────────────┐
                         │ Application      │
                         │ Services         │
                         └────────┬─────────┘
                                  │
                         ┌────────▼─────────┐
                         │ Session Resolver │
                         └────────┬─────────┘
                                  │
                         ┌────────▼─────────┐
                         │   Run Manager    │
                         └────────┬─────────┘
                                  │
                         ┌────────▼─────────┐
                         │  Agent Runtime   │
                         └────────┬─────────┘
                                  │
               ┌──────────────────┼───────────────────┐
               │                  │                   │
               ▼                  ▼                   ▼
        ContextBuilder      ToolExecutor        SubAgentRunner
               │                  │                   │
               │          ┌───────┼───────┐           │
               │          ▼       ▼       ▼           │
               │        Registry Policy Artifacts      │
               │                                      │
               └──────────────┬───────────────────────┘
                              │
                     ┌────────▼────────┐
                     │    Stores       │
                     ├─────────────────┤
                     │ SessionStore    │
                     │ RunStore        │
                     │ MessageStore    │
                     │ CheckpointStore │
                     │ MemoryStore     │
                     │ ArtifactStore   │
                     └─────────────────┘
```

---

# 162. Final Runtime Sequence

The full execution path should eventually be traceable as:

```text
1. Gateway receives user message.

2. Gateway authenticates / identifies user.

3. Gateway creates MessageEnvelope.

4. Application resolves the session.

5. Application checks session authorization.

6. RunManager creates a Run.

7. RunManager acquires the session lock.

8. Runtime loads session state.

9. Runtime appends HumanMessage.

10. Runtime builds model context.

11. Model receives system + memory + summary + recent messages.

12. Model returns AIMessage.

13. Runtime appends AIMessage.

14. If no tool calls:
      publish final event
      persist
      mark completed
      release lock

15. If tool calls exist:
      ToolExecutor resolves them.

16. Policy checks permissions, concurrency and approval.

17. Independent calls execute concurrently.

18. Tool results are converted into ToolExecutionResult objects.

19. Large outputs go to ArtifactStore.

20. Compact results become ToolMessages.

21. ToolMessages are appended to state.

22. State is checkpointed.

23. Runtime loops back to the model.

24. Repeat until final response / limit / cancellation / approval wait / fatal error.

25. Final state is persisted.

26. Run is marked completed or failed.

27. Event stream closes.

28. Gateway renders the final result.
```

---

# 163. Clean Architecture in This Project

Do not interpret "clean architecture" as dozens of interfaces.

For this project it means:

```text
frameworks at the edges
clear domain concepts in the middle
infrastructure behind protocols
explicit state ownership
side effects at known boundaries
```

A small runtime that you understand is cleaner than a large framework abstraction that you cannot reason about.

---

# 164. Implementation Sequence

This order matters. Build in layers.

## Phase 0 — Project skeleton

Create:

```text
pyproject.toml
src/personal_agent/
tests/
```

Install only what is necessary for the model/tool loop and testing.

Acceptance:

```bash
uv run pytest
```

works.

---

## Phase 1 — Model + messages

Implement:

```text
models/factory.py
core/state.py
```

Verify:

```python
model = init_chat_model(...)
response = await model.ainvoke([
    HumanMessage(content="Hello")
])
```

Then verify tool calling:

```python
response = await model.bind_tools(tools).ainvoke(...)
```

Acceptance:

- model works
- tool calls appear in `AIMessage.tool_calls`
- provider-specific formats do not leak into your runtime

---

## Phase 2 — Tool registry

Implement:

```text
tools/registry.py
```

Test:

```text
register tool
get tool
reject duplicate names
reject unknown tool
```

No real execution yet.

---

## Phase 3 — Tool executor

Implement:

```text
tools/executor.py
tools/policies.py
```

Features:

```text
lookup
validate
execute
error conversion
ToolMessage creation
timeout
basic concurrency
```

Test with fake tools.

---

## Phase 4 — Minimal agent loop

Implement:

```text
core/agent.py
core/runtime.py
core/errors.py
```

Required loop:

```text
HumanMessage
    ↓
model
    ↓
AIMessage
    ↓
tool_calls?
   ├── no → return
   └── yes
        ↓
   execute tools
        ↓
   ToolMessages
        ↓
   model again
```

Acceptance test:

```text
user → model → tool → ToolMessage → model → answer
```

Stop here and make sure you understand every line.

---

## Phase 5 — Event system

Implement:

```text
core/events.py
```

Add:

```text
run.started
tool.started
tool.completed
assistant.message
run.completed
run.failed
```

Acceptance:

```python
async for event in runtime.stream(...):
    print(event)
```

---

## Phase 6 — Session model

Implement:

```text
core/session.py
persistence/base.py
```

Start with in-memory stores.

Acceptance:

```text
request 1
 ↓
session

request 2
 ↓
same session
 ↓
conversation continues
```

---

## Phase 7 — SQLite persistence

Persist:

```text
sessions
messages
runs
```

Acceptance:

```text
send messages
kill process
restart
load session
continue
```

---

## Phase 8 — Checkpoints

Add:

```text
checkpoints
tool_executions
```

Checkpoint at semantic boundaries.

Simulate a process crash and verify recovery.

---

## Phase 9 — Run manager

Introduce:

```text
RunManager
RunHandle
```

Now make these operations first-class:

```text
start
resume
cancel
recover
```

---

## Phase 10 — Cancellation + limits

Implement:

```text
CancellationToken
AgentLimits
timeouts
step limits
tool-call limits
runtime deadlines
```

Test each path explicitly.

---

## Phase 11 — Workspace + terminal

Implement:

```text
tools/builtins/terminal.py
tools/builtins/filesystem.py
artifacts/local.py
```

Use a temporary test workspace.

Do not run filesystem tests against your real home directory.

---

## Phase 12 — Artifacts

Move large tool output outside model context.

Test:

```text
huge command output
 ↓
artifact
 ↓
short ToolMessage
```

---

## Phase 13 — Context management

Start with recent-window selection.

Then add:

```text
summary + recent messages
```

Do not add vector search yet.

---

## Phase 14 — Planning / TODOs

Add `TodoItem` and simple TODO tools after the state and compaction model are clear.

---

## Phase 15 — Subagents

Implement:

```text
subagents/registry.py
subagents/delegation.py
```

First:

```text
one parent
one child
```

Then:

```text
multiple parallel children
```

Verify depth and child limits.

---

## Phase 16 — Persistent memory

Implement:

```text
memory/store.py
memory/models.py
tools/builtins/memory.py
```

Start with:

```text
remember
recall
forget
```

---

## Phase 17 — Web gateway

Implement:

```text
gateways/base.py
gateways/web/
```

Use HTTP for commands and SSE for run events.

---

## Phase 18 — TUI

Implement the TUI strictly as an event consumer and application client.

It should not know how tools or models work.

---

## Phase 19 — Telegram

Implement Telegram after the core and other gateway path are stable.

This gives you the cleanest test surface before adding external-message quirks and formatting constraints.

---

## Phase 20 — Approval

Add approval for dangerous tools once terminal execution is stable.

---

## Phase 21 — Observability

Add structured logs, timing, metrics and tracing.

---

## Phase 22 — Hardening

Test:

```text
process crash
database failure
duplicate gateway message
provider timeout
provider 429
tool timeout
tool exception
concurrent session access
subagent failure
subagent timeout
cancellation
large output
large conversation
gateway reconnect
approval race
```

---

# 165. First Milestone

Your first actual milestone should be tiny:

```python
agent = Agent(
    name="personal",
    model="openai:gpt-5.5",
    tools=[calculator],
    system_prompt="You are a helpful assistant.",
)

runtime = AgentRuntime(...)

result = await runtime.run(
    session_id="test",
    user_id="local",
    message="Calculate 12 * 13",
)
```

Internally:

```text
HumanMessage
 ↓
model
 ↓
calculator tool call
 ↓
ToolMessage
 ↓
model
 ↓
AIMessage
```

Nothing else.

---

# 166. Second Milestone

Then add:

```text
terminal
filesystem
persistent sessions
```

You should be able to have:

```text
User:
Inspect this repository.

Agent:
I found a Python application.

User:
What did you find earlier?

Agent:
...
```

Restart the process and the session must still exist.

---

# 167. Third Milestone

Add:

```text
subagents
parallel tools
event streaming
```

Example:

```text
Parent
 ├── researcher
 └── coder
```

The parent receives compact child results rather than full transcripts.

---

# 168. Fourth Milestone

Add:

```text
Web
TUI
Telegram
```

All use the same persistent session and event protocol.

---

# 169. Fifth Milestone

Add:

```text
resume
approval
context compaction
long-horizon task state
```

At this point you have the essential characteristics of a lightweight DeepAgents-inspired personal runtime without cloning DeepAgents.

---

# 170. Design Decisions / ADRs Worth Recording

Keep a few short architecture decision records.

## ADR 001 — Agent vs Session

```text
Agent = reusable configuration
Session = durable conversational context
```

## ADR 002 — Explicit loop

```text
The first runtime is a simple async model/tool loop because
understanding execution is a project requirement.
```

## ADR 003 — Event protocol

```text
Gateways consume AgentEvent rather than LangChain messages because
runtime events are a user-interface contract, not a model-context contract.
```

## ADR 004 — Artifacts

```text
Large outputs are stored outside model messages to preserve context efficiency.
```

## ADR 005 — Subagents as tools

```text
Delegation uses the existing tool execution path so concurrency,
errors, limits and events are reused.
```

---

# 171. Recommended "Do Not Build Yet" List

Keep this list visible while coding:

```text
[ ] Generic middleware framework
[ ] Vector database
[ ] Automatic memory extraction
[ ] Skill system
[ ] Distributed queue
[ ] Kubernetes
[ ] Multi-tenant cloud architecture
[ ] Plugin marketplace
[ ] Graph-based workflow compiler
[ ] Model router
[ ] Browser automation
[ ] Voice gateway
[ ] Mobile application
[ ] Autonomous scheduler
```

Whenever you think you need one, ask:

```text
What concrete current requirement forces this abstraction?
```

If there is no concrete answer, defer it.

---

# 172. Suggested Git Milestones

Use commits that correspond to concepts:

```text
feat: initialize project
feat: add model factory
feat: add tool registry
feat: add tool executor
feat: implement agent loop
feat: add runtime events
feat: add sessions
feat: persist sessions in sqlite
feat: add checkpoints
feat: add cancellation
feat: add terminal tools
feat: add artifacts
feat: add context compaction
feat: add subagents
feat: add memory
feat: add web gateway
feat: add tui gateway
feat: add telegram gateway
feat: add approval flow
```

This makes the project's evolution itself a learning artifact.

---

# 173. Suggested First Coding Session

Write only:

```text
models/factory.py
tools/registry.py
tools/executor.py
core/state.py
core/agent.py
core/runtime.py
```

Then write deterministic unit tests.

Your target is:

```text
input
 ↓
model
 ↓
tool call
 ↓
tool
 ↓
ToolMessage
 ↓
model
 ↓
output
```

Once it works, inspect the architecture before adding persistence.

---

# 174. Suggested Second Coding Session

Add:

```text
events.py
session.py
in-memory stores
```

Make streaming work.

---

# 175. Suggested Third Coding Session

Add:

```text
SQLite
Run
Checkpoint
resume
```

Deliberately kill a run and resume it.

This will expose the real complexity of stateful agents.

---

# 176. Suggested Fourth Coding Session

Add:

```text
filesystem
terminal
artifacts
```

Now the project becomes a practical coding agent.

---

# 177. Suggested Fifth Coding Session

Add:

```text
subagents
parallel execution
approval
```

Now you have the main DeepAgents-inspired behaviors you wanted, while still owning the implementation.

---

# 178. One Major Architectural Recommendation

Do **not** make the following design:

```python
Agent(
    state=state,
    memory=memory,
)
```

where those objects are mutable and live on the Agent forever.

Prefer:

```text
Agent
    = configuration

Session
    = durable identity

Run
    = execution

AgentState
    = current mutable execution state

Store
    = persistence
```

This distinction will make multi-gateway and concurrent execution much easier to manage.

---

# 179. Final Architecture Recommendation

The completed system should conceptually be:

```text
                      PERSONAL AGENT
                           │
                 ┌─────────▼─────────┐
                 │ Agent Definition  │
                 │                   │
                 │ model             │
                 │ tools             │
                 │ subagents         │
                 │ system prompt     │
                 │ limits            │
                 └─────────┬─────────┘
                           │
                  ┌────────▼────────┐
                  │ Agent Runtime   │
                  │                 │
                  │ model loop      │
                  │ tool execution  │
                  │ state           │
                  │ context         │
                  │ cancellation    │
                  └────────┬────────┘
                           │
        ┌──────────────────┼──────────────────┐
        │                  │                  │
        ▼                  ▼                  ▼
   Session/Run        Event Bus          Subagents
        │                  │                  │
        ▼                  ▼                  ▼
   Persistence       Web/TUI/Telegram    Child Runs
        │
 ┌──────┼─────────────┬──────────────┐
 │      │             │              │
 ▼      ▼             ▼              ▼
msgs checkpoints    memory       artifacts
```

This is the right level of ambition for a personal DeepAgents-inspired runtime: **small enough to understand completely, structured enough that every major feature can be added without rewriting the core.**

---

# 180. Final Rule for Adding Features

When adding a feature, classify it first:

```text
Does this describe the agent?
    → AgentDefinition

Does this describe a conversation?
    → Session

Does this describe one execution?
    → Run / AgentState

Does this execute something?
    → Tool

Does this persist information?
    → Store

Does this communicate progress?
    → Event

Does this present the system to a user?
    → Gateway

Does this delegate independent work?
    → Subagent
```

If a new feature does not fit cleanly, reconsider the design before introducing another generic abstraction.

---

# 181. Minimum Viable Architecture

The entire first meaningful system can remain this small:

```text
Agent
 ├── model
 ├── tools
 ├── subagents
 ├── system_prompt
 └── limits

AgentRuntime
 ├── model runner
 ├── tool executor
 ├── context builder
 ├── state machine
 └── event bus

Stores
 ├── sessions
 ├── messages
 ├── runs
 └── checkpoints

Built-in Tools
 ├── terminal
 ├── filesystem
 ├── search
 └── memory

Subagents
 └── delegate

Gateways
 ├── TUI
 ├── Web
 └── Telegram
```

That is enough to build a real personal long-horizon coding agent.

---

# 182. One-Line Build Order

Keep this list visible while coding:

```text
1. model
2. messages
3. tool registry
4. tool executor
5. agent loop
6. events
7. sessions
8. SQLite persistence
9. checkpoints
10. run manager
11. cancellation/limits
12. filesystem
13. terminal
14. artifacts
15. context compaction
16. planning
17. subagents
18. memory
19. web
20. TUI
21. Telegram
22. approval
23. observability
24. hardening
```

Do not reorder these casually; each layer should make the next one easier.

---

# Appendix A — Suggested Initial Type Definitions

These are intentionally not a final implementation.

```python
@dataclass(frozen=True)
class AgentLimits:
    max_steps: int = 50
    max_tool_calls: int = 100
    max_runtime_seconds: int = 1800
    max_concurrent_tools: int = 8
    max_subagent_depth: int = 2
    max_subagents: int = 8


@dataclass(frozen=True)
class SubAgentSpec:
    name: str
    description: str
    agent: "Agent"


@dataclass(frozen=True)
class Agent:
    name: str
    model: str | BaseChatModel
    tools: tuple[BaseTool, ...] = ()
    subagents: tuple[SubAgentSpec, ...] = ()
    system_prompt: str = ""
    limits: AgentLimits = AgentLimits()


@dataclass
class AgentState:
    messages: list[BaseMessage]
    step_count: int = 0
    tool_call_count: int = 0
    todos: list["TodoItem"] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Session:
    id: str
    owner_id: str
    agent_name: str
    title: str | None
    status: str
    version: int
    metadata: dict[str, Any]


@dataclass
class Run:
    id: str
    session_id: str
    agent_name: str
    status: str
    parent_run_id: str | None
    parent_tool_call_id: str | None
    step_count: int
    tool_call_count: int
    started_at: datetime
    finished_at: datetime | None
    error: str | None


@dataclass(frozen=True)
class AgentEvent:
    id: str
    type: str
    run_id: str
    session_id: str
    sequence: int
    timestamp: datetime
    data: dict[str, Any]
```

The exact fields will evolve. The important part is the separation of responsibilities.

---

# Appendix B — Runtime Mental Model

When debugging the system, ask:

```text
What is the current SESSION?
What is the current RUN?
What is the current STATE?
What was the last MODEL message?
What TOOL CALLS were requested?
Which TOOLS are executing?
What was CHECKPOINTED?
What EVENTS were emitted?
What does the USER actually see?
What does the MODEL actually see?
```

Those questions will usually take you directly to the source of an execution bug.

---

# Appendix C — Current Reference Notes

The design uses these current LangChain concepts deliberately:

- `init_chat_model` for unified provider initialization.
- `BaseChatModel` as the runtime's model capability boundary.
- `bind_tools()` for standardized tool attachment.
- `AIMessage.tool_calls` for provider-neutral tool-call inspection.
- `ToolMessage.tool_call_id` for pairing tool results with requests.
- `BaseTool` as the standard tool interface.

The current DeepAgents implementation is useful primarily as a reference for **separation of responsibilities**: the harness assembles behavior around a lower-level agent runtime. Your implementation should borrow the architectural idea while remaining materially smaller and easier to understand.

---

# Appendix D — Final Heuristic

Every time you are about to introduce a large abstraction, ask:

```text
What concrete duplication or failure does this abstraction remove?
```

If the answer is unclear, prefer direct code.

For this project, clarity is the architectural feature.

Build the model/tool loop first. Add durability. Add events. Add real tools. Add subagents. Add gateways. Add advanced memory and orchestration only when the simpler system proves that it needs them.
