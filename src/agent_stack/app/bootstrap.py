from __future__ import annotations

from dotenv import load_dotenv

from agent_stack.app.application import Application
from agent_stack.app.config import AppConfig
from agent_stack.core.agent import Agent
from agent_stack.core.factory import AgentFactory
from agent_stack.core.session import InMemorySessionStore

load_dotenv()

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful personal assistant. Answer the user's request directly and "
    "concisely. Use the tools available to you when they help."
)


def build_application(config: AppConfig | None = None) -> Application:
    """Construct the full object graph.

    This is the single composition root: tools, stores, the agent definition, and the
    agent factory are wired here and nowhere else.
    """
    resolved = config or AppConfig.from_env()

    session_store = InMemorySessionStore()
    agent = Agent(
        name="assistant",
        model=resolved.model,
        tools=(),
        system_prompt=DEFAULT_SYSTEM_PROMPT,
    )
    factory = AgentFactory(session_store=session_store)

    return Application(
        config=resolved,
        agent=agent,
        factory=factory,
        session_store=session_store,
    )


def main() -> None:
    """Build the application and print a readiness line.

    No model call and no network access happen here.
    """
    application = build_application()
    print(f"agent-stack ready (agent={application.agent.name}, model={application.config.model})")
