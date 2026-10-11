from __future__ import annotations

from dotenv import load_dotenv

from agent_stack.app.application import Application
from agent_stack.app.config import AppConfig
from agent_stack.core.agent import Agent
from agent_stack.core.event_bus import InMemoryEventBus
from agent_stack.core.factory import AgentFactory
from agent_stack.core.locks import SessionLockManager
from agent_stack.core.run import RunManager
from agent_stack.core.workspace import WorkspacePolicy
from agent_stack.persistence.conversation import FileConversationStore
from agent_stack.persistence.sqlite import (
    SqliteCheckpointStore,
    SqliteDatabase,
    SqliteRunStore,
    SqliteSessionStore,
)
from agent_stack.tools.builtins.filesystem import filesystem_tools
from agent_stack.tools.builtins.terminal import run_command_tool

load_dotenv()

DEFAULT_SYSTEM_PROMPT = """\
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
"""


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

    db = SqliteDatabase(resolved.db_path)
    conversation = FileConversationStore(sessions_root)
    session_store = SqliteSessionStore(db, conversation)
    run_store = SqliteRunStore(db)
    checkpoint_store = SqliteCheckpointStore(db)
    event_bus = InMemoryEventBus()
    locks = SessionLockManager()

    policy = WorkspacePolicy(root=workspace)

    agent = Agent(
        name="assistant",
        model=resolved.model,
        tools=(*filesystem_tools(policy), run_command_tool(policy)),
        system_prompt=DEFAULT_SYSTEM_PROMPT,
    )
    factory = AgentFactory(
        session_store=session_store,
        event_bus=event_bus,
        session_lock=locks,
    )
    runtime = factory.create(agent)
    run_manager = RunManager(
        runtime=runtime,
        run_store=run_store,
        checkpoint_store=checkpoint_store,
        event_bus=event_bus,
    )

    return Application(
        config=resolved,
        agent=agent,
        factory=factory,
        session_store=session_store,
        run_manager=run_manager,
        event_bus=event_bus,
    )


def main() -> None:
    """Build the application and print a readiness line.

    No model call and no network access happen here.
    """
    application = build_application()
    print(f"agent-stack ready (agent={application.agent.name}, model={application.config.model})")
