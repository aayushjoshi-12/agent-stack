from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv
from langchain_core.language_models import BaseChatModel

from agent_stack.app.application import Application
from agent_stack.app.config import AppConfig
from agent_stack.artifacts.local import LocalArtifactStore
from agent_stack.context.composer import SystemPromptComposer
from agent_stack.context.instructions import InstructionsLoader
from agent_stack.context.summarizer import LLMSummarizer
from agent_stack.core.agent import Agent, SubAgentSpec
from agent_stack.core.approval import ApprovalBroker, ToolPolicy
from agent_stack.core.context import ContextBuilder
from agent_stack.core.errors import ConfigurationError
from agent_stack.core.factory import AgentFactory
from agent_stack.core.limits import AgentLimits
from agent_stack.core.locks import SessionLockManager
from agent_stack.core.run import RunManager
from agent_stack.core.workspace import WorkspacePolicy
from agent_stack.memory import MemoryIndex, memory_tools
from agent_stack.memory.ideas import IdeaInbox, idea_tools
from agent_stack.memory.store import SqliteMemoryStore
from agent_stack.models.factory import build_model
from agent_stack.observability.audit import AuditLogger, audit_log_path
from agent_stack.observability.logging import EventLogger, configure_logging
from agent_stack.observability.metrics import Metrics
from agent_stack.observability.sinks import FanoutSink
from agent_stack.persistence.changes import LocalChangeStore
from agent_stack.persistence.conversation import FileConversationStore
from agent_stack.persistence.event_log import EventStoreBus, SqliteEventLogStore
from agent_stack.persistence.sqlite import (
    SqliteCheckpointStore,
    SqliteDatabase,
    SqliteRunStore,
    SqliteSessionStore,
)
from agent_stack.persistence.todos import SqliteTodoStore
from agent_stack.skills import SkillLoader, SkillRegistry, skill_tools
from agent_stack.subagents.delegation import control_tools
from agent_stack.tools.builtins.artifacts import artifact_tools
from agent_stack.tools.builtins.filesystem import filesystem_tools
from agent_stack.tools.builtins.memory import persistent_memory_tools
from agent_stack.tools.builtins.terminal import run_command_tool
from agent_stack.tools.builtins.todos import todo_tools

load_dotenv()

#: Repo root (``src/agent_stack/app/bootstrap.py`` → up four levels).
_REPO_ROOT = Path(__file__).resolve().parents[3]

DEFAULT_SYSTEM_PROMPT = """\
You are a helpful personal assistant. Answer the user's request directly and
concisely — no preamble, no recap of what you did unless asked.

Tools:
- Prefer the dedicated tools (list_directory, read_file, write_file, edit_file,
  search_files, grep_files) over run_command for the same job: they are safer, scoped
  to the workspace, and easier to review.
- To read or explore the codebase without changing it, delegate to the `explorer`
  subagent with `delegate` instead of doing the reads yourself. While an `explorer`
  run is active you can check it with `subagent_status`, steer it with
  `subagent_feed`, or stop it with `subagent_interrupt`.
- When you make several independent tool calls, put them in a single message so
  they run in parallel. Chain dependent commands with `&&` inside run_command.
- Read only the parts of files you need. Use search_files to find files by name and
  grep_files to find text inside files (TODOs, symbols, error messages) instead of
  guessing paths. search_files never looks inside files.
- edit_file replaces the single unique occurrence of `old` with `new`. If the
  text appears more than once, widen `old` with surrounding context; if it
  appears zero times, re-read the file and retry exactly.
- If a skill matches the task, invoke it before generating any other response.
  Never mention a skill without invoking it, and never guess a skill name.

Doing a task (coding, setup, fixes):
- Work in small steps and check each one by running it. A task is done when you have
  run the real thing and seen it work, not when the code looks right.
- Before you stop, list every requirement in the request and check each one, the
  awkward cases first (cancellation, errors, empty input, limits, cleanup), by
  running a command that exercises it. Fix what fails, then stop.
- Decide, act, verify. If you catch yourself going back and forth on the same
  point, pick the best option, run it, and let the result settle it.
- Leave your results in place. Do not delete, reset or "clean up" what you built or
  tested (files, repos, services, data) unless the task says to; what you made is what
  gets checked.
- Long jobs (builds, installs, training): start them in the background with output to a
  log, then wait with one command that sleeps 60-120 seconds and prints the log tail,
  instead of polling every few seconds. Do other useful work while you wait.
- Print only what you need. Never dump a large file or image to the terminal; show a
  slice (head, tail, grep) or a summary.
- Do the work yourself instead of listing steps for the user, unless you truly
  cannot (no access, no permission); then say exactly what is blocked and why.

Code style:
- Follow the repo's AGENTS.md. Don't create planning or analysis documents
  unless the user asks.
- Default to no comments in code — one short line max when one is needed.
"""

EXPLORER_SYSTEM_PROMPT = """\
You are a read-only codebase explorer. Investigate exactly what was asked using
list_directory, read_file, search_files (names) and grep_files (contents), then report concise findings with the
file paths that justify them. You cannot modify anything.
"""

#: The read-only slice of the filesystem toolset handed to the explorer subagent.
_READ_ONLY_TOOLS = frozenset({"list_directory", "read_file", "search_files", "grep_files"})


def _title_model(name: str | None) -> BaseChatModel | None:
    """The small model that names conversations, or ``None`` (titles then stay the first message).

    Failing to build it (no API key for its provider) must never stop the app from starting.
    """
    if name is None:
        return None
    try:
        return build_model(name)
    except ConfigurationError:
        return None


def build_application(config: AppConfig | None = None) -> Application:
    """Construct the full object graph.

    This is the single composition root: infrastructure, stores, the agent definition,
    and the agent factory are wired here and nowhere else. Sessions live in
    ``<workspace>/.agent/sessions/`` (files are the source of truth); SQLite is only the
    index. No model call and no network access happen while wiring.
    """
    resolved = config or AppConfig.from_env()

    workspace = resolved.workspace
    workspace.mkdir(parents=True, exist_ok=True)
    sessions_root = workspace / ".agent" / "sessions"
    sessions_root.mkdir(parents=True, exist_ok=True)

    db = SqliteDatabase(resolved.database)
    conversation = FileConversationStore(sessions_root)
    event_log = SqliteEventLogStore(db)
    session_store = SqliteSessionStore(db, conversation, event_log)
    run_store = SqliteRunStore(db)
    checkpoint_store = SqliteCheckpointStore(db)
    audit = AuditLogger(audit_log_path(workspace))
    metrics = Metrics()
    configure_logging(workspace / ".agent" / "logs" / "agent.jsonl")
    event_bus = EventStoreBus(event_log, audit=FanoutSink(audit, EventLogger(), metrics))
    locks = SessionLockManager()

    policy = WorkspacePolicy(root=workspace)
    artifacts = LocalArtifactStore(workspace / ".agent" / "artifacts")
    change_store = LocalChangeStore(db, workspace / ".agent" / "changes")
    instructions = InstructionsLoader(global_dir=resolved.home)
    fs_tools = filesystem_tools(policy, change_store, nested_instructions=instructions)

    # Context wiring (P16e): instructions + skill metadata + the memory index become
    # resident system parts, and a ContextBuilder bounds the message window. Discovery is
    # non-strict so one non-conforming skill (e.g. a vendored one) cannot break every
    # root; user-global skill folders are added only through `AGENT_STACK_SKILLS_PATH`. Memory resolves `<workspace>/.agent/memory/`, falling back to a bare
    # `<workspace>/MEMORY.md`; the root is created only when a topic is written.
    skills = SkillRegistry(
        SkillLoader(
            [
                workspace / ".agent" / "skills",
                workspace / ".agents" / "skills",
                workspace / ".claude" / "skills",
                _REPO_ROOT / "skills",
                _REPO_ROOT / ".claude" / "skills",
                *resolved.skill_roots,
            ],
            strict=False,
        )
    )
    memory = MemoryIndex(workspace)
    todo_store = SqliteTodoStore(db)
    composer = SystemPromptComposer(
        instructions=instructions,
        skills=skills,
        memory=memory,
        workspace_root=workspace,
        todos=todo_store,
    )
    small = _title_model(resolved.small_model)
    context_builder = ContextBuilder(
        # the token budget decides what is kept; a 40-message window made long tasks forget
        recent_window=10_000,
        max_tokens=resolved.context_budget,
        summarizer=LLMSummarizer(small) if small is not None else None,
    )

    explorer = Agent(
        name="explorer",
        model=resolved.small_model or resolved.model,  # read-only searching: the cheap model
        tools=tuple(tool for tool in fs_tools if tool.name in _READ_ONLY_TOOLS),
        system_prompt=EXPLORER_SYSTEM_PROMPT,
    )
    agent = Agent(
        name="assistant",
        model=resolved.model,
        tools=(
            *fs_tools,
            run_command_tool(policy, confine=resolved.shell_confined),
            *control_tools(),
            *skill_tools(skills),
            *memory_tools(memory),
            *idea_tools(IdeaInbox(resolved.home)),
            *persistent_memory_tools(SqliteMemoryStore(db)),
            *todo_tools(todo_store),
            *artifact_tools(artifacts),
        ),
        subagents=(
            SubAgentSpec(
                name="explorer",
                description="Read-only codebase exploration.",
                agent=explorer,
            ),
        ),
        system_prompt=DEFAULT_SYSTEM_PROMPT,
        limits=AgentLimits(
            max_steps=resolved.max_steps,
            max_tool_calls=2 * resolved.max_steps,
            max_runtime_seconds=resolved.max_runtime_seconds,
        ),
        tool_policy=ToolPolicy(frozenset(resolved.approval_tools)),
    )
    title_model = small
    approvals = ApprovalBroker()
    factory = AgentFactory(
        session_store=session_store,
        event_bus=event_bus,
        session_lock=locks,
        context_builder=context_builder,
        system_parts=composer,
        approvals=approvals,
        artifacts=artifacts,
    )
    runtime = factory.create(agent)
    run_manager = RunManager(
        runtime=runtime,
        run_store=run_store,
        checkpoint_store=checkpoint_store,
        event_bus=event_bus,
        agent_factory=factory,
    )

    return Application(
        config=resolved,
        agent=agent,
        factory=factory,
        session_store=session_store,
        run_manager=run_manager,
        event_bus=event_bus,
        approvals=approvals,
        metrics=metrics,
        event_log=event_log,
        change_store=change_store,
        title_model=title_model,
    )
