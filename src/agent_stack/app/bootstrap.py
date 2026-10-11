from __future__ import annotations

from dotenv import load_dotenv

from agent_stack.app.application import Application
from agent_stack.app.config import AppConfig
from agent_stack.core.agent import Agent
from agent_stack.core.event_bus import InMemoryEventBus
from agent_stack.core.factory import AgentFactory
from agent_stack.core.locks import SessionLockManager
from agent_stack.core.run import RunManager
from agent_stack.persistence.conversation import FileConversationStore
from agent_stack.persistence.sqlite import (
    SqliteCheckpointStore,
    SqliteDatabase,
    SqliteRunStore,
    SqliteSessionStore,
)

load_dotenv()

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful personal assistant. Answer the user's request directly and "
    "concisely. Use the tools available to you when they help."
)


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

    agent = Agent(
        name="assistant",
        model=resolved.model,
        tools=(),
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
