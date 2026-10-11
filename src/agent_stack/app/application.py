from __future__ import annotations

from dataclasses import dataclass

from agent_stack.app.config import AppConfig
from agent_stack.core.agent import Agent
from agent_stack.core.factory import AgentFactory
from agent_stack.core.session import SessionStore


@dataclass
class Application:
    """The stable boundary gateways talk to.

    It exposes the wired object graph; gateways never import ``core`` directly.
    """

    config: AppConfig
    agent: Agent
    factory: AgentFactory
    session_store: SessionStore
